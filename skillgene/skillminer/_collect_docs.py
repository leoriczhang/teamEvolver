#!/usr/bin/env python3
"""重新收集物流客服主题公开 PDF：下载 -> fitz 抽取文本层 -> 只保留有正文的。

用途：本地验证数据集（data/validation/），不随开源仓库发布。
每个来源标注 URL 与用途，便于开源前逐份核对许可。
"""
import os
import ssl
import glob
import urllib.request
import urllib.parse

import fitz  # PyMuPDF

DEST = "data/validation/logistics_customer_service_docs"
PDFDIR = os.path.join(DEST, "pdfs")
TXTDIR = os.path.join(DEST, "extracted_text")
os.makedirs(PDFDIR, exist_ok=True)
os.makedirs(TXTDIR, exist_ok=True)

# (序号_文件名, url, 用途) —— 均为「物流/快递客服岗位实操」文档：话术、服务规范、售后、培训、投诉流程
SOURCES = [
    ("01_菜鸟客诉处理商家培训.pdf",
     "http://download.taobaocdn.com/freedom/24861/pdf/p1ad5vcblr1q1o101k13hrokvbj4.pdf",
     "物流客诉：咨询工单、投诉工单流程与注意事项、投诉类型与赔付规则、逆向流程、结算对账"),
    ("02_快递客服主动服务异常处理话术表.pdf",
     "http://download.taobaocdn.com/freedom/17063/pdf/zhudongfuwu.pdf",
     "快递异常（破损/延迟/信息有误/超区/自提）分级处理方案与对客展示话术"),
    ("03_金牌售后服务指南.pdf",
     "https://alime-kc.oss-cn-hangzhou.aliyuncs.com/kc/kc-attachment/kc-oss-1697778022745-%E9%87%91%E7%89%8C%E5%94%AE%E5%90%8E%E6%9C%8D%E5%8A%A1%E6%8C%87%E5%8D%97.pdf",
     "售后客服处理物流异常、仅退款、补发、协商话术与操作指引"),
    ("04_菜鸟仓配中小件新商家客诉培训.pdf",
     "http://download.taobaocdn.com/freedom/68786/pdf/p1d8iaombvfrh1k4f1f55rh915bu4.pdf",
     "物流客服：服务渠道/服务中心、咨询工单、投诉规则详解、异常场景与FAQ（催件/催退/查件/破损延迟）"),
    ("05_电子商务物流配送操作规范手册.pdf",
     "https://m.book118.com/try_down/648041103027007033.pdf",
     "客户服务规范、投诉处理流程/分类/反馈、客户满意度调查"),
    ("06_客服服务用语与电话沟通规范.pdf",
     "http://office.teacheredu.cn/zaixianyulan/teacheredu/webroot/files/201808/article/37004/201899367244599023.pdf",
     "客服坐席开头/问候/无法听清/客户抱怨投诉/结束等各场景规范服务用语与电话礼仪（话术库）"),
    ("07_电子商务客户服务规范.pdf",
     "http://www.zjol.com.cn/att/0/07/78/69/7786923_229936.pdf",
     "浙江省地方标准：客服人员/服务礼仪（开场白/欢迎语）、售中售后服务内容与程序、物流查询与投诉、退换货、满意度评价与改进"),
    ("08_客服工单处理与回复话术规范.pdf",
     "https://netmarket.oss-cn-hangzhou.aliyuncs.com/4e161751b469421985667b59d470b582.pdf",
     "客服工单流转状态与标准回复话术样例（安抚等待/需用户反馈/转单核对）、投诉与纠纷处理规范"),
    ("09_圆通速递客服培训手册.pdf",
     "https://m.book118.com/try_down/768013057064006060.pdf",
     "快递公司客服岗：服务规范、接听/查件/催件/投诉应答流程与话术、客诉分级处理"),
    ("10_快递业务投诉处理及反馈机制手册.pdf",
     "https://m.book118.com/try_down/466144151001010241.pdf",
     "快递投诉受理渠道、投诉分类与分级、处理时限、反馈与回访机制、赔偿与升级流程"),
    ("11_淘宝客服培训方案.pdf",
     "https://m.book118.com/try_down/766021233243010140.pdf",
     "客服优质用语、投诉应对方法（发泄/委婉否认/转化/主动担责）、服务用语标准与开头问候/无法听清/抱怨投诉/结束语规范、中差评处理"),
    ("12_电商平台客服培训手册.pdf",
     "https://m.book118.com/try_down/666104144101010211.pdf",
     "客服服务流程、售前售中售后、退换货处理、物流跟踪查询、客户投诉分类与处理流程（接收/分类/调查/方案/执行/反馈/改进）、满意度调查"),
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def download(name, url):
    out = os.path.join(PDFDIR, name)
    # 对路径中的非 ASCII / 空格做百分号编码（保留已有的 % 转义与分隔符）
    parts = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(parts.path, safe="/%")
    url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        data = r.read()
    with open(out, "wb") as f:
        f.write(data)
    return out, len(data)


def extract(pdf_path):
    base = os.path.basename(pdf_path)
    stem = base[:-4]
    d = fitz.open(pdf_path)
    parts = [f"# {stem}\n"]
    total = 0
    for i, pg in enumerate(d):
        t = pg.get_text("text").strip()
        if t:
            parts.append(f"\n<!-- p.{i+1} -->\n{t}")
            total += len(t)
    d.close()
    md = "\n".join(parts) + "\n"
    outp = os.path.join(TXTDIR, stem + ".md")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(md)
    return total, outp


if __name__ == "__main__":
    for name, url, _purpose in SOURCES:
        stem = name[:-4]
        if os.path.exists(os.path.join(TXTDIR, stem + ".md")) and os.path.getsize(
                os.path.join(TXTDIR, stem + ".md")) > 800:
            print(f"SKIP   {name}  已有抽取结果")
            continue
        try:
            out, n = download(name, url)
            head = open(out, "rb").read(5)
            if not head.startswith(b"%PDF"):
                print(f"NOTPDF {name}  {n} bytes head={head!r}  -> 删除")
                os.remove(out)
                continue
            chars, txt = extract(out)
            if chars < 300:
                print(f"NOTEXT {name}  {n} bytes 文本={chars}  -> 扫描件/无文本层，保留PDF待OCR")
            else:
                print(f"OK     {name}  {n} bytes 文本={chars} -> {os.path.basename(txt)}")
        except Exception as e:
            print(f"ERR    {name}  {type(e).__name__}: {e}")
