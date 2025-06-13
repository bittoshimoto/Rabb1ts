import binascii
from decimal import Decimal as D

from config import Config
from nicefetcher import utils


def calculate_reward(zero_count, max_zero_count):
    """
    Calculates the reward based on the number of leading zeros.

    Args:
        zero_count (int): Number of zeros at the beginning of the txid
        max_zero_count (int): Highest zero_count observed in this block

    Returns:
        int: Reward amount in atomic units
    """
    if zero_count < Config()["MIN_ZERO_COUNT"]:
        return 0
    reward = D(Config()["MAX_REWARD"]) / D(16 ** (max_zero_count - zero_count))
    return int(reward)


def calculate_distribution(value, output_values):
    """
    Distributes a total value among outputs proportionally.

    Args:
        value (int): Total amount to distribute
        output_values (list[int]): Values of the candidate outputs

    Returns:
        list[int]: Per-output distributed amounts
    """
    total_output = sum(output_values)
    if total_output == 0:
        return [0] * len(output_values)

    distribution = [
        int(D(value) * (D(output_value) / D(total_output)))
        for output_value in output_values
    ]
    total_distributed = sum(distribution)

    # if there's a remainder, give it to the first output
    if total_distributed < value:
        distribution[0] += value - total_distributed

    return distribution


def generate_movements(tx, quantity, output_values):
    """
    Builds the list of UTXO movements (where to credit).

    Args:
        tx (dict): Transaction dict
        quantity (int): Total amount to distribute (inputs + reward)
        output_values (list[int]): Values of tx["vout"] for weighting

    Returns:
        list[dict]: Each with keys "utxo_id" and "quantity"
    """
    movements = []
    distribution = calculate_distribution(quantity, output_values)

    # select only non-OP_RETURN outputs
    valid_outputs = [out for out in tx["vout"] if not out["is_op_return"]]

    # if more than one, drop the last one from distribution
    if len(valid_outputs) > 1:
        valid_outputs = valid_outputs[:-1]

    for i, out in enumerate(valid_outputs):
        if i < len(distribution):
            movements.append({
                "utxo_id": out["utxo_id"],
                "quantity": distribution[i],
            })

    return movements


def process_block_unified(block, mhin_store):
    """
    Process a block: compute rewards, roll inputs/rewards forward,
    and write everything into the MhinStore.

    Args:
        block (dict): Block dict from the fetcher
        mhin_store (MhinStore): Storage/indexing backend
    """
    height = block["height"]
    mhin_store.start_block(height, block["block_hash"])

    for tx in block["tx"]:
        # skip coinbase
        if "coinbase" in tx["vin"][0]:
            continue

        # collect non-OP_RETURN outputs
        output_values = [out["value"] for out in tx["vout"] if not out["is_op_return"]]
        if not output_values:
            continue

        # if there are multiple UTXOs, drop the last one from reward distribution
        if len(output_values) > 1:
            output_values.pop()

        zero_count = tx["zero_count"]
        max_zero = block["max_zero_count"]

        # DEBUG: show every tx that meets the minimum zero threshold
        if zero_count >= Config()["MIN_ZERO_COUNT"]:
            nice_hash = utils.inverse_hash(
                binascii.hexlify(tx["tx_id"]).decode()
            )
            print(
                f"DEBUG: block {height} tx {nice_hash} → "
                f"zero_count={zero_count}, max_zero_count={max_zero}"
            )

        # compute reward
        reward = calculate_reward(zero_count, max_zero)
        if reward > 0:
            nice_hash = utils.inverse_hash(
                binascii.hexlify(tx["tx_id"]).decode()
            )
            print(f"{reward} RB1TS rewarded for {nice_hash} (block {height})")

        # start the transaction in MhinStore (even if reward is zero, to handle UTXO movements)
        mhin_store.start_transaction(tx["tx_id"], reward)

        # gather inputs
        total_in = 0
        for vin in tx["vin"]:
            total_in += mhin_store.pop_balance(vin["utxo_id"])

        # distribute inputs + reward
        total_to_distribute = total_in + reward
        if total_to_distribute > 0:
            movements = generate_movements(tx, total_to_distribute, output_values)
            for mv in movements:
                if mv["quantity"] > 0:
                    mhin_store.add_balance(mv["utxo_id"], mv["quantity"])

        mhin_store.end_transaction()

    mhin_store.end_block()
