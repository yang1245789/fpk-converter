#!/usr/bin/env python3
import http.server
import socketserver
import os

# 设置端口
PORT = 8080

# 切换到 /workspace 目录
os.chdir("/workspace")

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # 添加 CORS 头部
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

# 启动服务器
Handler = MyHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"服务器已启动，在手机浏览器访问: http://localhost:{PORT}")
    print(f"下载文件: fpk-converter.fpk")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")