#!/usr/bin/env python3
import http.server
import socketserver
import os

PORT = 8000

os.chdir("/workspace")

Handler = http.server.SimpleHTTPRequestHandler

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"服务器运行在 http://localhost:{PORT}")
    print(f"可以通过 Preview 访问，或者浏览器打开下载链接！")
    print("Ctrl+C 停止服务器")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器停止")
