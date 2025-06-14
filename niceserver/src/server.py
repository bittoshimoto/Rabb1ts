from decimal import Decimal as D
import binascii

from flask import Flask, render_template, url_for, request
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash


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
    """
    utxo is "<le‐hex>:<vout>".  We extract the LE hex,
    invert back to big-endian TXID, bold/truncate, and link.
    """
    if isinstance(utxo, bytes):
        le_hex = binascii.hexlify(utxo).decode()
    else:
        parts = utxo.split(":")
        # if utxo was ":abcdef" then parts[0]=="" and parts[1] is the hex
        le_hex = parts[0] if parts[0] else parts[1]

    try:
        txid = utils.inverse_hash(le_hex)
    except Exception:
        txid = le_hex

    if full:
        disp = bold_zero(txid)
    else:
        disp = bold_zero(f"{txid[:12]}…{txid[-12:]}")

    linked = f'<a href="https://b1texplorer.com/tx/{txid}" target="_blank">{disp}</a>'
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


if __name__ == "__main__":
    Config().set_network("mainnet")
    app.run(host="127.0.0.1", debug=True)