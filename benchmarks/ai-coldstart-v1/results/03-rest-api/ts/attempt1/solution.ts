import express from "express";

const app = express();

const users: Record<number, { id: number; name: string; email: string }> = {
  1: { id: 1, name: "Ada", email: "ada@example.com" },
  2: { id: 2, name: "Ben", email: "ben@example.com" },
  3: { id: 3, name: "Cara", email: "cara@example.com" },
};

app.get("/users/:id", (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id) || id <= 0) {
    res.status(400).json({ error: "bad id" });
    return;
  }
  const user = users[id];
  if (!user) {
    res.status(404).json({ error: "not found" });
    return;
  }
  res.json(user);
});

app.listen(8931);
