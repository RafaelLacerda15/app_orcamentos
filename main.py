import os

from orcamentos import create_app


app = create_app()


if __name__ == "__main__":
    debug_enabled = (os.getenv("FLASK_DEBUG") or "").strip() == "1"
    app.run(debug=debug_enabled, port=8000)
