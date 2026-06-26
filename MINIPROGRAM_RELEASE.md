# 微信小程序发布说明

## 项目结构

- `app.py`：媒体解析与代理后端，必须部署在服务器上。
- `wsgi.py`：生产环境 WSGI 入口。
- `miniprogram/`：可导入微信开发者工具的小程序前端。
- `templates/`：原网页前端，可继续单独使用。

微信小程序不能直接运行 Flask、Playwright 或 FFmpeg，因此不能把整个 Python
项目打进小程序包。小程序只负责界面、请求、播放和保存；解析与转码必须由
HTTPS 后端完成。

## 1. 部署后端

建议使用 Linux 云服务器或容器，至少预留 2 GB 内存。抖音 Live 图解析会启动
无头 Chromium，小红书 HEVC 预览会调用 FFmpeg，因此纯函数计算平台可能超时。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
gunicorn -w 2 -t 600 -b 127.0.0.1:9880 wsgi:app
```

使用 Nginx/Caddy 配置 HTTPS 反向代理，并确认以下地址可访问：

```text
https://api.example.com/api/health
```

Windows 服务器可使用：

```powershell
waitress-serve --listen=127.0.0.1:9880 wsgi:app
```

不要在生产环境开启 `APP_DEBUG`。

## 2. 配置小程序

1. 在微信公众平台注册小程序并取得 AppID。
2. 修改 `miniprogram/project.config.json` 中的 `appid`。
3. 修改 `miniprogram/config.js`：

```js
API_BASE_URL: "https://api.example.com"
```

4. 在小程序后台的“开发管理 → 开发设置 → 服务器域名”中，将后端域名配置到：

   - request 合法域名
   - downloadFile 合法域名

5. 用微信开发者工具导入 `miniprogram/` 目录。
6. 真机测试解析、视频播放、图片保存、视频保存和相册授权。
7. 上传代码，提交体验版，完成隐私保护指引与服务类目后提交审核。

开发阶段可以关闭开发者工具的域名校验，但正式版不能依赖该选项。

## 3. 发布前必须处理

- 使用备案且证书有效的 HTTPS 域名，不能使用 localhost。
- 准备用户隐私保护指引，说明剪贴板读取、网络请求及相册保存用途。
- 首次使用保存功能时，由用户主动触发相册授权。
- 增加用户协议、版权投诉入口和内容使用提示。
- 不要宣传为绕过平台权限、批量搬运或盗用内容的工具。
- 解析规则依赖第三方公开页面，平台改版或风控会导致临时失效，需要持续维护。

“去水印/下载第三方平台内容”可能涉及平台条款、著作权以及审核类目风险。正式
提交前应确认你拥有处理和保存相关内容的权利，并按实际业务选择服务类目和描述。

## 官方文档

- [小程序网络能力](https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html)
- [wx.downloadFile](https://developers.weixin.qq.com/miniprogram/dev/api/network/download/wx.downloadFile.html)
- [小程序协同工作和发布](https://developers.weixin.qq.com/miniprogram/dev/framework/quickstart/release.html)
