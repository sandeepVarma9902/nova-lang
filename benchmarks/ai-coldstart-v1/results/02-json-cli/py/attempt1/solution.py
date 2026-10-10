import json
import sys


def is_valid(u):
    return (
        isinstance(u, dict)
        and isinstance(u.get("name"), str)
        and isinstance(u.get("age"), int)
        and not isinstance(u.get("age"), bool)
    )


def main():
    try:
        with open("data.json", encoding="utf8") as f:
            raw = f.read()
    except FileNotFoundError:
        print("error: data.json not found", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("error: malformed JSON", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, list):
        print("error: malformed JSON", file=sys.stderr)
        sys.exit(1)
    users = [u for u in data if is_valid(u)]
    count = len(users)
    age_sum = sum(u["age"] for u in users)
    avg = age_sum / count if count else 0
    no_email = [u["name"] for u in users if u.get("email") is None]
    print(f"users: {count}")
    print(f"avg_age: {avg:.1f}")
    print(f"no_email: {', '.join(no_email) if no_email else 'none'}")


main()
