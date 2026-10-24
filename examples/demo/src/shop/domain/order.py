from dataclasses import dataclass


@dataclass
class Order:
    id: int
    total_cents: int

    def with_discount(self, percent: int) -> Order:
        return Order(self.id, self.total_cents * (100 - percent) // 100)
