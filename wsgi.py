from app import create_app

app = create_app()

if __name__ == "__main__":
    # 本地无 Docker 时的开发入口：python wsgi.py
    app.run(host="0.0.0.0", port=8000, debug=False)
