from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            return {
                "available": bool(tables),
                "table_count": len(tables),
                "tables": [{"table_number": t["table_number"], "capacity": t["capacity"]}
                           for t in tables],
            }
        self._tool_run("restaurant", work)
'''

new = '''    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            if not tables:
                return {"message": "No tables are available at that time. "
                                   "Offer a different time."}
            first = tables[0]
            return {
                "message": f"{len(tables)} tables are available. "
                           f"Offer to book table {first['table_number']} "
                           f"(seats {first['capacity']}) at the requested time.",
                "tables": [{"table_number": t["table_number"], "capacity": t["capacity"]}
                           for t in tables],
            }
        self._tool_run("restaurant", work)
'''

if old not in text:
    print("ERROR: old _tool_check_tables not found.")
    print("Search for 'def _tool_check_tables' in server.py and paste the current code.")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Patched _tool_check_tables.")
