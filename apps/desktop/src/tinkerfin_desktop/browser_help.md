# 本机浏览器

通过 `tinkerfin-browser` 操作当前会话的独立浏览器。

```sh
tinkerfin-browser tools
tinkerfin-browser describe browser_navigate
tinkerfin-browser call browser_navigate '{"url":"https://example.com"}'
tinkerfin-browser call browser_snapshot '{}'
```

执行动作前，用 `describe` 读取该工具的参数。点击、填写等操作使用最新快照中的元素引用；页面变化后重新读取快照。

截图和下载文件保存在当前项目目录。用文件工具读取结果，不将图片的 base64 内容放入命令参数。

浏览器资料按会话保留，登录会持续到后续调用。命令不接受浏览器路径、用户资料、外部调试地址或其他会话标识。工具报错或执行被取消时，先核对页面与外部操作结果，再决定是否重试。
