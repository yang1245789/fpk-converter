# 飞牛NAS 视频自动转码工具

自动监测文件夹中视频文件，智能转码为 H.265 节省存储空间。

## 安装

### 方法一：下载安装包
从 [Release](https://github.com/yang1245789/fpk-converter/releases) 下载 `fpkconverter.fpk`，在飞牛 NAS 应用中心手动安装。

### 方法二：自行打包
```bash
git clone https://github.com/yang1245789/fpk-converter.git
cd fpk-converter
fnpack build
# 生成 fpkconverter.fpk
```

## 使用
安装后访问 `http://你的NAS_IP:5000`

## 功能
- 自动监测视频文件
- H.265 转码节省 30-50% 空间
- Intel Quick Sync GPU 加速
- Web UI 管理界面