import csv
import html
import os
import re
import threading
import time
import queue
from pathlib import Path
from urllib.parse import urljoin, quote

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


CN_BASE = "http://www.cnho.mil.cn"
CN_LIST_PAGE = f"{CN_BASE}/tg/pdf"
CN_LIST_API = f"{CN_BASE}/getPdfList"
CN_DOWNLOAD_API = f"{CN_BASE}/downPdf"

UK_BASE = "https://msi.admiralty.co.uk"
UK_WEEKLY = f"{UK_BASE}/NoticesToMariners/Weekly"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0 Safari/537.36"
)


class DownloaderError(Exception):
    pass


def is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def safe_remove(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("航海通告批量下载器")
        self.root.geometry("820x650")
        self.root.minsize(760, 580)

        self.msg_queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()

        self.type_var = tk.StringVar(value="中国版航海通告")
        self.year_var = tk.StringVar(value=str(time.localtime().tm_year))
        self.dir_var = tk.StringVar(
            value=str(Path.home() / "Admiralty")
        )
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="成功：0    跳过：0    失败：0")
        self.progress_var = tk.DoubleVar(value=0)

        self.success = 0
        self.skip = 0
        self.fail = 0
        self.failed_rows = []

        self.build_ui()
        self.root.after(100, self.process_queue)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(
            outer,
            text="航海通告批量下载器",
            font=("Microsoft YaHei UI", 18, "bold")
        )
        title.pack(anchor="w", pady=(0, 14))

        form = ttk.LabelFrame(outer, text="下载设置", padding=12)
        form.pack(fill="x")

        ttk.Label(form, text="下载类型：").grid(row=0, column=0, sticky="w", pady=6)
        type_box = ttk.Combobox(
            form,
            textvariable=self.type_var,
            values=["中国版航海通告", "英国版 UKHO Weekly Notices"],
            state="readonly",
            width=34
        )
        type_box.grid(row=0, column=1, sticky="w", pady=6)

        ttk.Label(form, text="年份：").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.year_var, width=20).grid(
            row=1, column=1, sticky="w", pady=6
        )

        ttk.Label(form, text="保存目录：").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.dir_var, width=55).grid(
            row=2, column=1, sticky="we", pady=6
        )
        ttk.Button(form, text="浏览...", command=self.choose_dir).grid(
            row=2, column=2, padx=(8, 0), pady=6
        )
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=12)

        self.start_btn = ttk.Button(
            buttons, text="开始下载", command=self.start
        )
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(
            buttons, text="停止", command=self.stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=8)

        ttk.Button(
            buttons, text="打开保存目录", command=self.open_dir
        ).pack(side="right")

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x")

        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(
            status_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate"
        )
        self.progress.pack(fill="x", pady=7)
        ttk.Label(status_frame, textvariable=self.count_var).pack(anchor="w")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))

        self.log = tk.Text(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            state="disabled"
        )
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log.yview
        )
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if d:
            self.dir_var.set(d)

    def open_dir(self):
        d = Path(self.dir_var.get()).expanduser()
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def log_msg(self, text):
        self.msg_queue.put(("log", text))

    def status(self, text):
        self.msg_queue.put(("status", text))

    def stats(self):
        self.msg_queue.put(("stats", None))

    def update_progress(self, current, total):
        value = 0 if total <= 0 else current * 100 / total
        self.msg_queue.put(("progress", value))

    def start(self):
        if self.worker and self.worker.is_alive():
            return

        year_text = self.year_var.get().strip()
        if not re.fullmatch(r"\d{4}", year_text):
            messagebox.showerror("输入错误", "年份必须是 4 位数字，例如 2026。")
            return

        save_root = self.dir_var.get().strip()
        if not save_root:
            messagebox.showerror("输入错误", "保存目录不能为空。")
            return

        self.success = self.skip = self.fail = 0
        self.failed_rows = []
        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set("准备开始...")
        self.log_clear()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        mode = self.type_var.get()
        year = int(year_text)
        root_dir = Path(save_root).expanduser()

        self.worker = threading.Thread(
            target=self.run_download,
            args=(mode, year, root_dir),
            daemon=True
        )
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status("正在停止，请等待当前请求结束...")
        self.stop_btn.config(state="disabled")

    def run_download(self, mode, year, root_dir):
        try:
            save_dir = root_dir / str(year)
            save_dir.mkdir(parents=True, exist_ok=True)

            if mode == "中国版航海通告":
                self.download_cn(year, save_dir)
            else:
                self.download_ukho(year, save_dir)

            if self.failed_rows:
                self.write_failed_csv(save_dir)

            if self.stop_event.is_set():
                self.status("已停止")
                self.log_msg("下载任务已停止。")
            else:
                self.status("下载完成")
                self.log_msg("")
                self.log_msg("==================================================")
                self.log_msg("下载完成")
                self.log_msg(
                    f"成功：{self.success}    跳过：{self.skip}    失败：{self.fail}"
                )
                self.log_msg(f"保存目录：{save_dir}")
                if self.failed_rows:
                    self.log_msg(f"失败记录：{save_dir / 'failed.csv'}")

        except Exception as e:
            self.status("任务失败")
            self.log_msg(f"错误：{e}")
        finally:
            self.msg_queue.put(("finished", None))

    def make_session(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        })
        return s

    def download_cn(self, year, save_dir):
        session = self.make_session()
        session.headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })

        self.status("正在访问中国版网站...")
        self.log_msg("正在访问中国海军海道测量局网站...")
        r = session.get(CN_LIST_PAGE, timeout=30)
        r.raise_for_status()
        self.log_msg("网站访问成功。")

        self.status(f"正在获取 {year} 年航海通告列表...")
        r = session.post(
            CN_LIST_API,
            data={"year": str(year)},
            timeout=30
        )
        r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            raise DownloaderError(
                "服务器返回的内容不是有效 JSON。\n"
                + r.text[:500]
            )

        if not data.get("success"):
            raise DownloaderError(
                "服务器返回 success=false。\n" + r.text[:500]
            )

        items = data.get("result") or []
        if not items:
            self.log_msg(f"{year} 年没有找到航海通告。")
            self.progress(100, 100)
            return

        self.log_msg(f"找到 {len(items)} 期航海通告。")
        total = len(items)

        for index, item in enumerate(items, 1):
            if self.stop_event.is_set():
                break

            notice_year = str(item.get("year", year))
            notice_num = str(item.get("noticeNum", ""))
            item_id = str(item.get("id", ""))

            filename = f"{notice_year}-{notice_num}.pdf"
            local_file = save_dir / filename

            self.status(f"正在处理：{filename}")
            self.log_msg(f"[{index}/{total}] {filename}")

            if local_file.exists() and local_file.stat().st_size > 0:
                self.skip += 1
                self.log_msg("  文件已存在，跳过。")
                self.update_progress(index, total)
                self.stats()
                continue

            if not item_id:
                self.fail += 1
                self.failed_rows.append({
                    "type": "CN",
                    "year": notice_year,
                    "notice": notice_num,
                    "week": "",
                    "id": "",
                    "url": "",
                    "error": "接口返回的 id 为空"
                })
                self.log_msg("  失败：接口返回的 id 为空。")
                self.update_progress(index, total)
                self.stats()
                continue

            url = f"{CN_DOWNLOAD_API}?id={quote(item_id, safe='')}"

            try:
                self.download_file(session, url, local_file)
                self.success += 1
                self.log_msg(
                    f"  下载成功 ✓  {local_file.stat().st_size / 1024 / 1024:.2f} MB"
                )
            except Exception as e:
                safe_remove(local_file)
                self.fail += 1
                self.failed_rows.append({
                    "type": "CN",
                    "year": notice_year,
                    "notice": notice_num,
                    "week": "",
                    "id": item_id,
                    "url": url,
                    "error": str(e)
                })
                self.log_msg(f"  下载失败 ✗  {e}")

            self.update_progress(index, total)
            self.stats()
            time.sleep(0.5)

    def download_ukho(self, year, save_dir):
        session = self.make_session()
        session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

        self.status("正在访问 UKHO 网站...")
        self.log_msg("正在访问 UKHO Weekly Notices...")
        r = session.get(UK_WEEKLY, timeout=30)
        r.raise_for_status()

        token_match = re.search(
            r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"',
            r.text,
            re.I
        )
        if not token_match:
            # 容错：属性顺序或换行变化时再次尝试
            token_match = re.search(
                r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
                r.text,
                re.I
            )
        if not token_match:
            raise DownloaderError("无法找到 __RequestVerificationToken。")

        token = html.unescape(token_match.group(1))
        year_short = str(year)[-2:]
        self.log_msg("已获取 RequestVerificationToken。")

        total = 53
        for week in range(1, 54):
            if self.stop_event.is_set():
                break

            week_text = f"{week:02d}"
            filename = f"{week_text}wknm{year_short}.pdf"
            local_file = save_dir / filename

            self.status(f"正在处理：Week {week} / 53")
            self.log_msg(f"[{week}/53] {filename}")

            if local_file.exists() and local_file.stat().st_size > 0:
                self.skip += 1
                self.log_msg("  文件已存在，跳过。")
                self.update_progress(week, total)
                self.stats()
                continue

            post_data = {
                "year": str(year),
                "week": str(week),
                "__RequestVerificationToken": token
            }

            try:
                response = session.post(
                    UK_WEEKLY,
                    data=post_data,
                    timeout=30
                )
                response.raise_for_status()
            except Exception as e:
                self.fail += 1
                self.failed_rows.append({
                    "type": "UKHO",
                    "year": str(year),
                    "notice": "",
                    "week": str(week),
                    "id": "",
                    "url": "",
                    "error": f"获取 Week 页面失败：{e}"
                })
                self.log_msg(f"  获取页面失败 ✗  {e}")
                self.update_progress(week, total)
                self.stats()
                time.sleep(1)
                continue

            download_url = self.find_ukho_download_url(
                response.text, filename
            )

            if not download_url:
                # UKHO 后期页面有时结构会变化，再做一次宽松搜索
                escaped = re.escape(filename)
                m = re.search(
                    rf'href=["\']([^"\']*?/NoticesToMariners/DownloadFile\?[^"\']*?fileName={escaped}[^"\']*)["\']',
                    response.text,
                    re.I
                )
                if m:
                    download_url = html.unescape(m.group(1))

            if not download_url:
                self.skip += 1
                self.log_msg("  未找到对应文件，跳过。")
                self.update_progress(week, total)
                self.stats()
                continue

            download_url = html.unescape(download_url)
            download_url = urljoin(UK_BASE, download_url)

            try:
                self.download_file(session, download_url, local_file)
                self.success += 1
                self.log_msg(
                    f"  下载成功 ✓  {local_file.stat().st_size / 1024 / 1024:.2f} MB"
                )
            except Exception as e:
                safe_remove(local_file)
                self.fail += 1
                self.failed_rows.append({
                    "type": "UKHO",
                    "year": str(year),
                    "notice": "",
                    "week": str(week),
                    "id": "",
                    "url": download_url,
                    "error": str(e)
                })
                self.log_msg(f"  下载失败 ✗  {e}")

            self.update_progress(week, total)
            self.stats()
            time.sleep(0.5)

    @staticmethod
    def find_ukho_download_url(page_html, filename):
        escaped = re.escape(filename)

        # 优先：直接在 href 中找到对应 filename
        pattern = (
            rf'href=["\']([^"\']*?/NoticesToMariners/DownloadFile\?'
            rf'[^"\']*?fileName={escaped}[^"\']*)["\']'
        )
        m = re.search(pattern, page_html, re.I)
        if m:
            return html.unescape(m.group(1))

        # 与原 PowerShell 一致：先找到 filename_1，再在附近找 DownloadFile
        stem = re.escape(filename[:-4])
        fm = re.search(
            rf'<td\s+id=["\']filename_1["\']\s*>\s*{stem}\s*</td>',
            page_html,
            re.I
        )
        if fm:
            section = page_html[fm.start():fm.start() + 10000]
            m = re.search(
                r'href=["\']([^"\']*?/NoticesToMariners/DownloadFile\?[^"\']*)["\']',
                section,
                re.I
            )
            if m:
                return html.unescape(m.group(1))

        return None

    @staticmethod
    def download_file(session, url, path):
        temp = path.with_suffix(path.suffix + ".part")
        safe_remove(temp)

        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()

            with temp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)

        if not temp.exists() or temp.stat().st_size <= 0:
            safe_remove(temp)
            raise DownloaderError("下载文件大小为 0。")

        if not is_pdf(temp):
            safe_remove(temp)
            raise DownloaderError("服务器返回的文件不是 PDF。")

        temp.replace(path)

    def write_failed_csv(self, save_dir):
        path = save_dir / "failed.csv"
        fields = ["type", "year", "notice", "week", "id", "url", "error"]
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.failed_rows)

    def log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def process_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log.config(state="normal")
                    self.log.insert("end", str(data) + "\n")
                    self.log.see("end")
                    self.log.config(state="disabled")
                elif kind == "status":
                    self.status_var.set(str(data))
                elif kind == "stats":
                    self.count_var.set(
                        f"成功：{self.success}    "
                        f"跳过：{self.skip}    "
                        f"失败：{self.fail}"
                    )
                elif kind == "progress":
                    self.progress_var.set(float(data))
                elif kind == "finished":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.count_var.set(
                        f"成功：{self.success}    "
                        f"跳过：{self.skip}    "
                        f"失败：{self.fail}"
                    )
        except queue.Empty:
            pass

        self.root.after(100, self.process_queue)


def main():
    root = tk.Tk()
    try:
        root.iconname("航海通告下载器")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
