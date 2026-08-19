# 航海通告批量下载器

一个 Windows GUI 工具，用于批量下载：

- 中国版航海通告
- UKHO Admiralty Weekly Notices to Mariners

## GitHub Actions 生成 EXE

1. 把本目录中的文件上传到 GitHub 仓库。
2. 进入仓库的 `Actions`。
3. 选择 `Build Windows EXE`。
4. 点击 `Run workflow`。
5. 等待构建完成。
6. 在 Actions 的运行结果底部下载 Artifact：
   `航海通告下载器-Windows`

解压后即可得到：

`航海通告下载器.exe`

## 本地运行

```bash
pip install -r requirements.txt
python main.py
```

## 功能

- 中国版：先建立 Session，再 POST `/getPdfList`，根据返回 ID 下载 `/downPdf?id=...`
- 英国版：建立 Session，获取 CSRF Token，逐周 POST Week 1~53，从页面查找对应 `DownloadFile`
- 已存在且非空的 PDF 自动跳过
- 下载后检查 `%PDF-` 文件头
- 下载失败保存到 `failed.csv`
- 使用临时 `.part` 文件，避免中途失败留下假 PDF
