import pandas as pd
from bs4 import BeautifulSoup
import os
import glob
import re
import warnings

# 屏蔽警告
warnings.filterwarnings("ignore", category=UserWarning)


def get_soup(file_path):
    if not os.path.exists(file_path): return None
    with open(file_path, 'r', encoding='iso-8859-1', errors='ignore') as f:
        return BeautifulSoup(f.read(), 'html.parser')


def clean_cell_data(val):
    if pd.isna(val): return None
    s = str(val).replace('\xa0', ' ').strip()
    s = re.sub(r'[\*\s]+$', '', s)
    if s in ['', '.', '-', 'nan', 'NaN', '0', '0.0']:
        return None
    try:
        return float(s.replace(',', ''))
    except ValueError:
        return s


def process_table_raw(table_html):
    """全表穿透扫描，防漏且精准剔除空列"""
    try:
        df_list = pd.read_html(str(table_html), header=None)
        if not df_list: return None
        df = df_list[0]
    except:
        return None

    if df.empty or len(df) <= 1: return None

    df = df.map(clean_cell_data)
    valid_cols = [0]  # 强制永远保留 A 列

    ignore_phrases = [
        'ista mielke', 'oil world', 'information provider', 'more details see', 'http',
        'production', 'stocks', 'supply', 'demand', 'crush', 'exports', 'imports',
        'oil', 'world', 'balance', 'commodity'
    ]

    for c in range(1, df.shape[1]):
        data_body = df.iloc[:, c]
        is_meaningful = False

        for cell in data_body:
            if cell is None: continue

            # 规则1：任何列，只要碰到真正的数字 (float)，直接保送通过！
            if isinstance(cell, float):
                is_meaningful = True
                break

            # 规则2：专门给 B 列特赦，保住类似 "Aug 31" 的日期标签
            elif c == 1 and isinstance(cell, str) and len(cell.strip()) > 1:
                cell_lower = cell.strip().lower()
                if not any(p in cell_lower for p in ignore_phrases):
                    is_meaningful = True
                    break

        if is_meaningful:
            valid_cols.append(c)

    df = df.iloc[:, valid_cols]
    return df if df.shape[1] > 1 else None


def extract_and_merge(file_path):
    soup = get_soup(file_path)
    if not soup: return None

    # ==========================================
    # 【核心反套娃逻辑】：发现框架网页，直接追踪 body 文件
    # ==========================================
    tables = soup.find_all('table')
    if not tables:
        # 寻找是否有 frame 标签，或者指向 _body 的链接
        frame = soup.find('frame', src=re.compile(r'_body\.htm', re.I))
        if not frame:
            frame = soup.find('a', href=re.compile(r'_body\.htm', re.I))

        if frame:
            target_file = frame.get('src') or frame.get('href')
            new_path = os.path.join(os.path.dirname(file_path), target_file)
            print(f"    [!] 识别到套娃框架，正在追踪真实数据源: {target_file}")
            # 递归调用自身，去处理那个隐藏的 body 文件
            return extract_and_merge(new_path)

        return None

    combined_elements = []

    for table in tables:
        title = ""
        prev = table.previous_element
        for _ in range(40):
            if not prev: break
            if prev.name in ['u', 'strong', 'b', 'font']:
                t = prev.get_text(strip=True)
                if len(t) > 5:
                    title = t
                    break
            prev = prev.previous_element

        df_cleaned = process_table_raw(table)

        if df_cleaned is not None:
            if title:
                title_df = pd.DataFrame([[title] + [None] * (df_cleaned.shape[1] - 1)])
                combined_elements.append(title_df)
            combined_elements.append(df_cleaned)
            combined_elements.append(pd.DataFrame([[None] * df_cleaned.shape[1]]))

    if combined_elements:
        return pd.concat([el.T.reset_index(drop=True).T for el in combined_elements], ignore_index=True)
    return None


def run_ultimate_importer():
    root_files = glob.glob("__* START.htm")
    if not root_files: return

    date_str = re.search(r"__(.*?) START", root_files[0]).group(1).strip()
    final_excel_name = f"油世界季度表-{date_str}.xlsx"
    print(f">>> 开始执行雷达导入: {final_excel_name}")

    root_soup = get_soup(root_files[0])
    all_data = []

    for section in root_soup.find_all('h3'):
        td = section.find_parent('td')
        if not td: continue
        for idx_link in td.find_all('a'):
            idx_file = idx_link.get('href')
            if not idx_file or not idx_file.lower().endswith('.htm'): continue

            idx_name = idx_link.get_text(strip=True)
            idx_soup = get_soup(idx_file)
            if not idx_soup: continue

            for link in idx_soup.find_all('a'):
                href = link.get('href', '')

                if 'stats/' in href.lower() and href.lower().endswith('.htm'):
                    report_title = link.get_text(strip=True)

                    prev_b = link.find_previous(['b', 'strong'])
                    current_category = idx_name

                    if prev_b:
                        cat_name = prev_b.get_text(strip=True)
                        if cat_name and "Copyright" not in cat_name and "OIL WORLD" not in cat_name:
                            if cat_name.lower() in ['oilseeds', 'oils & fats', 'oilmeals', 'oils & fats and biodiesel']:
                                current_category = f"{idx_name} - {cat_name}"
                            else:
                                current_category = cat_name

                    all_data.append({
                        "板块": section.get_text(strip=True),
                        "品种/国家": current_category,
                        "报表名称": report_title,
                        "数据文件名": href.split('/')[-1]
                    })

    if not all_data: return

    df_catalog = pd.DataFrame(all_data)
    writer = pd.ExcelWriter(final_excel_name, engine='xlsxwriter')
    df_catalog.to_excel(writer, sheet_name='目录', index=False)

    for htm_file in df_catalog['数据文件名'].unique():
        file_path = os.path.join("Stats", htm_file)
        sheet_name = htm_file.replace(".HTM", "").replace(".htm", "")

        df_final = extract_and_merge(file_path)

        if df_final is not None:
            df_final.iloc[:, 0] = df_final.iloc[:, 0].apply(lambda x: re.sub(r'[\.\s]+$', '', str(x)) if x else x)
            df_final.to_excel(writer, sheet_name=sheet_name, index=False, header=False)
            print(f"  [√] {sheet_name} 已处理")

    workbook = writer.book
    link_format = workbook.add_format({'font_color': 'blue', 'underline': 1})
    ws_menu = writer.sheets['目录']
    for row_num, filename in enumerate(df_catalog["数据文件名"]):
        sn = filename.replace(".HTM", "").replace(".htm", "")
        ws_menu.write_url(row_num + 1, 4, f"internal:'{sn}'!A1", link_format, string=str(filename))

    writer.close()
    print("\n【大功告成】！框架嵌套的隐藏数据也全部提取成功。")


if __name__ == "__main__":
    run_ultimate_importer()