import os

PORT = int(os.getenv("PORT", "8000"))

print(f"listening on {PORT}")
