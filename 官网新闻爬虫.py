#
# 第20行 MAX_PAGES_PER_SITE
# 第235行 tasks
# 为测试配置，实际使用时应修改
# 
import asyncio
import os
import re
import time
import random
import datetime
import pandas as pd
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler

# --- ⚙️ 配置中心 ---
INPUT_FILE = '公司新闻官网列表.xlsx'  # 你的输入文件
OUTPUT_ROOT = '新闻储存'        # 结果保存的根目录
MAX_PAGES_PER_SITE = 2                  # 每个网站最多翻多少页（防止死循环）###############################这里第一次先修改成2页试试
MAX_CONCURRENT_TABS = 1                  # 这是一个通用爬虫，建议设为1，串行爬取最稳定
TIMEOUT_SECONDS = 30                     # 单页加载超时时间

# 翻页关键词指纹（启发式算法的核心）
NEXT_PAGE_KEYWORDS = [
    '下一页', '下页', 'next', '>', '»', 'more', 'load more', '后一页', 'next page'
]

# --- 🛠️ 辅助函数 ---

def sanitize_filename(name):
    """清洗文件名，移除Windows/Linux不支持的字符"""
    if not name:
        return "Untitled"
    # 移除 / \ : * ? " < > | 以及换行符
    clean_name = re.sub(r'[\\/*?:"<>|\n\r]', '', name)
    # 限制长度，防止文件名过长
    return clean_name[:100].strip()

def extract_publish_date(html_content):
    """尝试从HTML中提取日期（这是一个难点，使用正则尝试匹配常见格式）"""
    # 匹配 2024-01-01, 2024/01/01, 2024年1月1日
    date_patterns = [
        r'\d{4}-\d{1,2}-\d{1,2}',
        r'\d{4}/\d{1,2}/\d{1,2}',
        r'\d{4}年\d{1,2}月\d{1,2}日'
    ]
    for pattern in date_patterns:
        match = re.search(pattern, html_content)
        if match:
            return match.group(0)
    return datetime.datetime.now().strftime("%Y-%m-%d") # 默认返回当天

async def save_markdown(company_name, news_data):
    """保存为 Markdown 文件，包含 Front Matter 元数据"""
    # 1. 创建公司文件夹
    company_dir = os.path.join(OUTPUT_ROOT, sanitize_filename(company_name))
    if not os.path.exists(company_dir):
        os.makedirs(company_dir)

    # 2. 准备文件内容
    filename = sanitize_filename(news_data['title']) + ".md"
    file_path = os.path.join(company_dir, filename)
    
    # 3. 构建 YAML Front Matter (元数据)
    front_matter = f"""---
title: "{news_data['title']}"
date: {news_data['publish_date']}
crawl_time: "{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
source_url: "{news_data['url']}"
company: "{company_name}"
---

"""
    # 4. 写入文件
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(front_matter)
            f.write(f"# {news_data['title']}\n\n")
            # Crawl4AI 的 markdown 默认包含图片链接 ![alt](url)
            f.write(news_data['content'])
        print(f"      💾 已保存: {filename}")
    except Exception as e:
        print(f"      ❌ 保存失败: {e}")

# --- 🕷️ 核心爬虫逻辑 ---

async def process_single_company(crawler, company_name, start_url):
    print(f"🏢 启动任务: {company_name}")
    print(f"   入口链接: {start_url}")
    
    current_url = start_url
    visited_urls = set() # 记录已访问的详情页，防止重复
    domain = urlparse(start_url).netloc
    
    for page in range(MAX_PAGES_PER_SITE):
        print(f"   📖 正在扫描第 {page + 1} 页...")
        
        try:
            # 1. 加载列表页
            result = await crawler.arun(url=current_url, magic=True) # magic=True 有助于规避部分反爬
            if not result.success:
                print("      ⚠️ 列表页加载失败")
                break
            
            soup = BeautifulSoup(result.html, 'html.parser')
            links = soup.find_all('a', href=True)
            
            # 2. 分离“详情页链接”和“翻页链接”
# --- 定义需要排除的干扰域名黑名单 ---
            BLACK_LIST_DOMAINS = [
                'miit.gov.cn', 'beian.mps.gov.cn', 'baidu.com', 'google.com', 
                'adservice', 'doubleclick', 'share', 'login'
            ]
            
            # --- 定义信任的外部媒体平台白名单 ---
            TRUSTED_EXTERNAL_DOMAINS = [
                'mp.weixin.qq.com',  # 微信公众号
                'content.eastmoney.com', # 东方财富
                'cls.cn', # 财联社
                'wallstreetcn.com', # 华尔街见闻
                'gov.cn',  # 政府官网
                'cninfo.com.cn'  # 巨潮资讯
            ]

            article_candidates = []
            next_page_url = None
            
            for link in links:
                text = link.get_text(strip=True)
                href = link['href']
                full_url = urljoin(current_url, href)
                parsed_url = urlparse(full_url)
                link_domain = parsed_url.netloc.lower()
                
                # --- A. 寻找翻页 (保持原有逻辑) ---
                if not next_page_url:
                    if any(kw == text.lower() or kw in text.lower() for kw in NEXT_PAGE_KEYWORDS):
                        if full_url != current_url and len(text) < 15:
                            next_page_url = full_url
                            continue

                # --- B. 寻找文章 (优化后的业务逻辑) ---
                
                # 1. 基础过滤：排除 JavaScript 脚本链接和空链接
                if not href or href.startswith('javascript') or '#' in href:
                    continue

                # 2. 域名检查逻辑
                is_internal = domain in link_domain
                is_trusted_external = any(td in link_domain for td in TRUSTED_EXTERNAL_DOMAINS)
                is_blacklisted = any(bd in link_domain for bd in BLACK_LIST_DOMAINS)

                # 3. 核心判断逻辑：
                # (是内部链接 OR 是信任的外部媒体) 
                # AND 标题长度符合新闻特征
                # AND 不在黑名单中
                if (is_internal or is_trusted_external or not is_blacklisted):
                    # 只有当标题文字超过 8 个字，才认为它是新闻（防止把导航栏的“产品展示”当成新闻）
                    if len(text) > 8 and full_url != current_url:
                        if full_url not in visited_urls:
                            article_candidates.append({
                                'title': text, 
                                'url': full_url,
                                'is_external': not is_internal # 标记是否为外部链接
                            })

            # 去重
            unique_articles = list({v['url']: v for v in article_candidates}.values())
            print(f"      👀 发现 {len(unique_articles)} 篇潜在新闻（含外部链接）")

            # 3. 逐个爬取详情页
            for article in unique_articles:
                try:
                    # 随机等待 1-3 秒，模拟人类，防止被封
                    await asyncio.sleep(random.uniform(1, 3))
                    
                    # 抓取详情
                    detail_res = await crawler.arun(url=article['url'], magic=True)
                    if detail_res.success and len(detail_res.markdown) > 50:
                        
                        # 提取日期 (使用正则在HTML里找)
                        pub_date = extract_publish_date(detail_res.html)
                        
                        news_data = {
                            'title': article['title'],
                            'url': article['url'],
                            'content': detail_res.markdown, # 图片链接已包含在 markdown 中
                            'publish_date': pub_date
                        }
                        
                        # 保存文件
                        await save_markdown(company_name, news_data)
                        visited_urls.add(article['url'])
                    else:
                        print(f"      ⚠️ 内容过短或抓取失败: {article['title'][:10]}")

                except Exception as e:
                    print(f"      ❌ 详情页出错: {e}")
            
            # 4. 执行翻页
            if next_page_url:
                print(f"      ⏭️ 翻页中: {next_page_url}")
                current_url = next_page_url
                await asyncio.sleep(2) # 翻页前多停一会
            else:
                print("      ⏹️ 没有发现更多页面，该对应公司任务结束。")
                break

        except Exception as e:
            print(f"   ❌ 列表页处理出错: {e}")
            break

async def main():
    # 1. 检查输入
    if not os.path.exists(INPUT_FILE):
        print(f"错误: 找不到文件 {INPUT_FILE}")
        return
    
    # 2. 读取 Excel
    print("📂 读取任务列表...")
    df = pd.read_excel(INPUT_FILE)
    
    # 对应你给出的列名: 'INSTITUTIONNAME' 和 '新闻页网址'
    # 1. 首先过滤掉 TRUEURL 列中的空值 (NaN)
    df = df.dropna(subset=['TRUEURL'])

    # 2. 定义需要排除的特定字符串列表
    exclude_list = ['#VALUE!', 'javascript:;']

    # 3. 过滤掉包含在排除列表中的行
    # ~ 表示取反，即保留“不在列表内”的行
    tasks = df[~df['TRUEURL'].isin(exclude_list)]
    
    tasks = tasks[0:20]############################################ 测试时先限制数量，正式运行可去掉这一整行
    print(f"🚀 共有 {len(tasks)} 个公司待处理。")
    print(f"📂 结果将保存在: ./{OUTPUT_ROOT}/")

    # 3. 启动爬虫上下文
    async with AsyncWebCrawler(verbose=False) as crawler:
        for index, row in tasks.iterrows():
            company_name = str(row['INSTITUTIONNAME']).strip()
            news_url = str(row['TRUEURL']).strip()
            
            # 简单的 URL 校验
            if not news_url.startswith('http'):
                print(f"⚠️ 跳过无效链接: {company_name} - {news_url}")
                continue

            await process_single_company(crawler, company_name, news_url)
            print("-" * 50)
            
            # 公司与公司之间的大间隔，防止IP被封
            await asyncio.sleep(5)

if __name__ == "__main__":
    # Windows 下如果出现 EventLoop 报错，需要加上这一句
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    asyncio.run(main())
