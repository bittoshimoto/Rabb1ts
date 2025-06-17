from decimal import Decimal as D
import binascii

from flask import Flask, render_template, url_for, request
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash

from sender import list_utxos, send_rb1ts, broadcast_tx
from config import Config
from queries import Rb1tsQueries
from nicefetcher import utils

app = Flask(__name__)
auth = HTTPBasicAuth()

users = {
    "admin": generate_password_hash("myassisnice2025"),
}

NETWORK_NAME = "mainnet"

Config().set_network(NETWORK_NAME)


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username


def bold_zero(value):
    if not value.startswith("0"):
        return value
    bold_value = f"<b>"
    closed = False
    for char in value:
        if char != "0" and not closed:
            bold_value += f"</b>{char}"
            closed = True
        else:
            bold_value += f"{char}"
    return bold_value


def display_utxo(utxo, full=False):
    if isinstance(utxo, bytes):
        hexstr = binascii.hexlify(utxo).decode("utf-8")
    else:
        hexstr = str(utxo)

    txid = utils.inverse_hash(hexstr)
    if full:
        disp = bold_zero(txid)
    else:
        disp = bold_zero(f"{txid[:12]}…{txid[-12:]}")

    # <-- use an f-string here! -->
    return f'<a href="https://blockbook.b1tcore.org/tx/{txid}" target="_blank">{disp}</a>'
    return linked


def display_quantity(quantity):
    value = D(quantity) / D(Config()["UNIT"])
    return "{0:.8f} RB1TS".format(value)


@app.route("/")
def home():
    queries = Rb1tsQueries()
    return render_template(
        "home.html",
        rewards=queries.get_latest_nicehashes(),
        stats=queries.get_stats(),
        display_utxo=display_utxo,
        display_quantity=display_quantity,
        css_url=url_for("static", filename="home.css"),
    )


@app.route("/protocol")
def protocol():
    return render_template(
        "protocol.html", css_url=url_for("static", filename="home.css")
    )


@app.route("/balances")
def balances():
    address = request.args.get("address")
    queries = Rb1tsQueries()
    balance = queries.get_balance_by_address(address)
    return render_template(
        "balances.html",
        display_utxo=display_utxo,
        display_quantity=display_quantity,
        balance=balance,
        css_url=url_for("static", filename="home.css"),
        address=address,
    )

@app.route('/send', methods=['GET', 'POST'])
def send():
    # …
    if request.method == 'GET' or ('confirm' not in request.form and 'broadcast' not in request.form):
        address = request.values.get('address', '')
        wif     = request.values.get('wif', '')
        # Grab as strings for the GET form, but override on POST preview
        total_rb = request.values.get('total_rb', '')
        send_rb  = request.values.get('send_rb', '')
        utxos = []
        txhex = None

        if address:
            utxos = list_utxos(address)

        # POST preview
        if request.method == 'POST' and address and wif and request.form.get('total_rb'):
            total_rb = float(request.form['total_rb'])
            send_rb  = float(request.form['send_rb'])
            utxo_index = int(request.form['utxo_index'])
            recv_addr  = request.form['recv_addr']
            rest_addr  = request.form['rest_addr']
            change_addr= request.form['change_addr']
            txhex, metadata = send_rb1ts(
                wif, address, total_rb, send_rb,
                utxo_index, recv_addr, rest_addr, change_addr
            )

        return render_template(
            'send.html',
            utxos=utxos,
            address=address,
            wif=wif,
            total_rb=total_rb,     # ← include both here
            send_rb=send_rb,       # ← so Jinja can compare them
            txhex=txhex,
            broadcast=False,
            css_url=url_for('static', filename='home.css'),
            display_quantity=display_quantity
        )

    # POST with confirm: perform broadcast or cancellation
    else:
        confirm = request.form.get('confirm')
        txhex = request.form.get('txhex')
        if confirm == 'yes':
            try:
                txid = broadcast_tx(txhex)
                success = True
                error = None
            except Exception as e:
                txid = None
                success = False
                error = str(e)
        else:
            txid = None
            success = False
            error = 'User cancelled'

        return render_template(
            'send.html',
            utxos=None,
            address=None,
            wif=None,
            txhex=None,
            broadcast=True,
            success=success,
            txid=txid,
            error=error,
            css_url=url_for('static', filename='home.css')
        )

if __name__ == "__main__":
    Config().set_network("mainnet")
    app.run(host="127.0.0.1")
