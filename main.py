import os

from orcamentos import create_app


app = create_app()


if __name__ == "__main__":
    port = int((os.getenv("PORT") or "1000").strip() or "1000")
    debug_enabled = (os.getenv("FLASK_DEBUG") or "").strip() == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
