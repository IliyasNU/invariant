from shop.domain.order import Order

ORDERS: dict[int, Order] = {}


def save(order: Order) -> None:
    ORDERS[order.id] = order
