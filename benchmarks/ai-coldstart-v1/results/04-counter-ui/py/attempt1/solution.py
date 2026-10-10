class Counter:
    def __init__(self, title: str):
        self.title = title
        self.count = 0

    def click(self) -> None:
        self.count += 1

    def render(self) -> str:
        return f"<div><h1>{self.title}</h1><p>{self.count}</p><button>Increment</button></div>"


c = Counter("Clicks")
print(c.render())
c.click()
print(c.render())
