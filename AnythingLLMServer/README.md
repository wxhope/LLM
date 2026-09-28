# AnythingLLMServer

一个单文件网页，把本地文件上传到 AnythingLLM 的**第一个工作区**并完成向量化嵌入。

## 用法

双击 `serve.bat`（会启动本地服务器并自动打开浏览器）。

或者手动：

```bash
python -m http.server 8080 --bind 127.0.0.1
# 然后打开 http://localhost:8080/
```

页面上选文件 → 点「上传并向量化」。

## 前置条件

AnythingLLM 桌面版必须在运行（`http://localhost:3001`）。

## 调用的接口

1. `GET  /api/v1/workspaces` → 取第一个工作区的 slug
2. `POST /api/v1/document/upload`（multipart，字段名 `file`）→ 返回 `documents[0].location`
3. `POST /api/v1/workspace/{slug}/update-embeddings`（`{"adds":[location],"deletes":[]}`）→ 向量化

第 2、3 步必须成对：**只上传不调用 update-embeddings，文档不会进入工作区。**

需通过本地 HTTP 服务器访问，不要直接双击 `index.html`（`file://` 下跨域可能被拦）。

## API 地址为什么写 127.0.0.1 而不是 localhost

**必须写 `http://127.0.0.1:3001`。** 在 WorkBuddy 的内置预览面板里打开时，
面板会给页面注入一条 CSP：

```
connect-src 'self' http://127.0.0.1:* https://*.map.qq.com ...
```

白名单里只有 `127.0.0.1`，**没有 `localhost`**。用 `localhost:3001` 会被 CSP 直接拦掉，
控制台报 `Refused to connect ... violates Content Security Policy`。

## 排错

| 现象 | 原因 |
| --- | --- |
| 控制台 `Refused to connect ... Content Security Policy` | API 地址写成了 `localhost`，改成 `127.0.0.1` |
| 提示 `API Key 无效（403）` | Key 不对，去 AnythingLLM 的 Developer API 重新生成 |
| `TypeError: Failed to fetch` | AnythingLLM 没启动，或页面是 `file://` 打开的 |
| 上传成功但文档没进工作区 | 第 3 步 `update-embeddings` 没执行成功 |
