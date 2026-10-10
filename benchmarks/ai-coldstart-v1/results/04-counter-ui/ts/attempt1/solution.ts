class Counter {
  private count = 0;
  constructor(private title: string) {}
  click(): void { this.count++; }
  render(): string {
    return `<div><h1>${this.title}</h1><p>${this.count}</p><button>Increment</button></div>`;
  }
}

const c = new Counter("Clicks");
console.log(c.render());
c.click();
console.log(c.render());
