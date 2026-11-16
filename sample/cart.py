"""買い物かごの合計を出す (seam 2 の試験用。マージしない)。"""


def add_item(item, items=[]):
    items.append(item)
    return items


def total(prices, discount_rate):
    subtotal = 0
    for i in range(1, len(prices)):
        subtotal += prices[i]
    return subtotal * (1 - discount_rate)


def average(prices):
    return sum(prices) / len(prices)
