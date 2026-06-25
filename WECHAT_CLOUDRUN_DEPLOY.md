# 当前项目部署到微信云托管

项目已经包含云托管所需的 `Dockerfile` 和 `.dockerignore`。后端容器监听
`8080` 端口，使用 Gunicorn 启动 Flask，并在镜像中安装 Chromium。

## 一、开通云托管

1. 登录[微信云托管控制台](https://cloud.weixin.qq.com/cloudrun/service)。
2. 使用你准备发布小程序的管理员微信扫码登录。
3. 创建云托管环境，记录环境 ID。
4. 在云开发控制台将该环境与准备发布的小程序关联。
5. 进入“服务管理”，新建服务：

   - 服务名称：`media-extractor`
   - 部署方式：本地代码/上传代码包
   - 服务类型：Web 服务
   - 监听端口：`8080`

## 二、上传代码

上传项目根目录 `D:\wgh\project\python`，不是只上传 `app.py`。

上传内容必须包含：

```text
Dockerfile
.dockerignore 
app.py
wsgi.py
requirements.txt 
templates/
```

`miniprogram/` 不需要进入后端镜像，已由 `.dockerignore` 排除。

## 三、服务配置建议

初次部署建议：

- CPU：2 核
- 内存：4 GB
- 最小实例数：测试期 `0`，正式使用 `1`
- 最大实例数：先设 `2`
- 端口：`8080`
- 请求超时：如果控制台支持配置，设为至少 `600` 秒
- 公网访问：保持开启，因为视频组件和 `wx.downloadFile` 需要直接访问媒体代理

环境变量：

```text
APP_HOST=0.0.0.0
APP_PORT=8080
APP_DEBUG=false
REQUEST_TIMEOUT=30
DOUYIN_BROWSER_PATH=/usr/bin/chromium
GUNICORN_WORKERS=1
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=600
```

不要增加多个 Gunicorn Worker。每个 Worker 都可能启动 Chromium，并且小红书
转码缓存锁只在单进程内有效；需要并发时优先让云托管增加容器实例。

## 四、部署后验证

部署成功后，在服务详情复制 HTTPS 访问域名，例如：

```text
https://media-extractor-xxxx.run.tcloudbase.com
```

浏览器访问：

```text
https://你的服务域名/api/health
```

正常返回：

```json
{
  "success": true,
  "service": "media-extractor",
  "platforms": ["douyin", "kuaishou", "xiaohongshu"]
}
```

然后测试解析：

```http
POST /api/extract
Content-Type: application/json

{"url": "一个分享链接"}
```

如果构建失败，在构建日志中重点搜索：

- `chromium`
- `pip install`
- `gunicorn`
- `No space left`

如果服务启动失败，确认控制台端口与 `APP_PORT` 都是 `8080`。

## 五、连接微信小程序

修改 `miniprogram/config.js`：

```js
module.exports = {
  CLOUD_ENV: "你的云托管环境ID",
  CLOUD_SERVICE: "media-extractor",
  API_BASE_URL: "https://你的云托管服务域名"
}
```

其中：

- `/api/extract` 使用 `wx.cloud.callContainer` 调用，不经过公网域名。
- 视频、图片预览及下载使用 `API_BASE_URL`，因为 `<video>`、`<image>` 和
  `wx.downloadFile` 需要可直接访问的 HTTPS URL。

在微信公众平台“开发管理 → 开发设置 → 服务器域名”中，把云托管服务域名加入：

- downloadFile 合法域名
- 如果你保留 `wx.request` 兜底，也加入 request 合法域名

然后使用微信开发者工具导入 `miniprogram/` 文件夹，真机测试。

## 六、当前项目在云托管上的注意事项

1. 抖音 Live 图会启动无头 Chromium，冷启动和首次解析较慢。
2. 小红书 HEVC 视频首次预览会转码，可能需要 10 秒以上。
3. `.preview_cache` 位于容器临时磁盘，实例释放或重新部署后缓存会消失。
4. 所有图片与视频通过云托管代理，公网流量会产生费用。
5. 建议设置调用频率限制，否则公开代理容易被滥用。
6. 正式审核前需要补充隐私保护指引、用户协议和版权投诉入口。

## 官方资料

- [CloudBase Python 快速开始](https://docs.cloudbase.net/run/quick-start/dockerize-python)
- [CloudBase 微信小程序访问云托管](https://docs.cloudbase.net/run/develop/access/mini)
- [CloudBase 云托管概述](https://docs.cloudbase.net/run/quick-start/introduce)
