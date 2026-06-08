#!/usr/bin/env python3
import sqlite3
from datetime import datetime


def view_log():
    db_path = 'fpk_converter.db'
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM processed_files ORDER BY processed_at DESC')
        results = cursor.fetchall()
        
        if not results:
            print("暂无处理记录")
            return
        
        print("=" * 120)
        print(f"{'ID':<4} {'文件路径':<60} {'原大小(MB)':<12} {'是否成功':<8} {'节省空间(MB)':<12} {'处理时间':<20}")
        print("=" * 120)
        
        total_saved = 0
        for row in results:
            file_id, filepath, file_size, processed_at, success, saved_size = row
            file_size_mb = file_size / (1024 * 1024)
            saved_size_mb = saved_size / (1024 * 1024) if saved_size else 0
            success_str = "✓" if success else "✗"
            total_saved += saved_size
            
            print(f"{file_id:<4} {filepath[:57]:<60} {file_size_mb:<12.2f} {success_str:<8} {saved_size_mb:<12.2f} {processed_at:<20}")
        
        print("=" * 120)
        print(f"总记录数: {len(results)}")
        print(f"总共节省空间: {total_saved / (1024 * 1024):.2f} MB")
        
        conn.close()
    except sqlite3.Error as e:
        print(f"数据库错误: {e}")


if __name__ == "__main__":
    view_log()
