#!/usr/bin/env python3
"""Check the bilingual Qwen3.6 story against its source data in native Chrome."""
import argparse
import json
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright


SLUG = 'more-than-speed'
TITLES = {
    'en': 'Twice the Context, No New Computer: A MacBook Meets an RTX 2080 Ti',
    'zh': '上下文翻倍，不必换机：让 MacBook 和家里的 2080 Ti 搭把手',
}


def scroll_top(page):
    page.evaluate("""() => {
        document.querySelector('main').scrollTop = 0;
        window.scrollTo(0, 0);
    }""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=f'http://127.0.0.1:65300/blog/{SLUG}/')
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--proxy', help='Optional browser proxy for public-site validation')
    args = parser.parse_args()
    base = args.url.split('/blog', 1)[0] + '/'
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'pageErrors': [], 'siteHttpErrors': [], 'complete': False}
    with sync_playwright() as playwright:
        options = {'channel': 'chrome', 'headless': True, 'args': ['--disable-gpu']}
        if args.proxy:
            options['proxy'] = {'server': args.proxy}
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            page = context.new_page()
            page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
            page.on('response', lambda response: report['siteHttpErrors'].append(response.url)
                    if response.status >= 400 and response.url.startswith(base) else None)
            page.goto(args.url)
            page.wait_for_load_state('networkidle')
            article = page.locator(f'article[data-blog-post="{SLUG}"]')
            expect(article).to_be_visible()
            page.screenshot(path=str(args.artifacts / 'arrival-desktop.png'))
            assert page.locator('article[data-blog-post]:visible').count() == 1
            assert page.locator('[data-blog-index]').is_hidden()

            # Both languages quote the immutable replay dataset, not duplicated constants.
            report['metrics'] = page.evaluate("""async ({url, slug}) => {
                const {qwen36ReplayData: data} = await import(url);
                const root = document.querySelector(`article[data-blog-post="${slug}"]`);
                const checked = [];
                for (const [attribute, field] of [['data-blog-rate', 'tpotSeconds'], ['data-blog-ttft', 'ttftSeconds']]) {
                    for (const node of root.querySelectorAll(`[${attribute}]`)) {
                        const key = node.getAttribute(attribute);
                        const [prompt, mode, host] = key.split('/');
                        const value = data.prompts[prompt].modes[mode].results[host][field];
                        const expected = (field === 'tpotSeconds' ? 1 / value : value).toFixed(2);
                        if (node.textContent.trim() !== expected) throw new Error(`${key}: expected ${expected}`);
                        checked.push({key, field, displayed: expected});
                    }
                }
                for (const panel of root.querySelectorAll('[data-blog-language-panel]')) {
                    for (const host of ['home', 'server']) {
                        const gains = Object.values(data.prompts).map(prompt => {
                            const results = prompt.modes.off.results;
                            return results[host].tpotSeconds / results.prima.tpotSeconds;
                        });
                        const text = `${Math.min(...gains).toFixed(2)}–${Math.max(...gains).toFixed(2)}`;
                        if (!panel.textContent.includes(text)) throw new Error(`Missing measured gain ${text}`);
                    }
                }
                return checked;
            }""", {'url': urljoin(base, 'src/qwen36-replay-data-20260908.js'), 'slug': SLUG})
            assert len(report['metrics']) == 34
            report['checks'].append('32 rate citations + 2 TTFT citations and both gain ranges match replay data')
            report['mac128Metrics'] = page.evaluate("""async ({url, slug}) => {
                const response = await fetch(url);
                if (!response.ok) throw new Error(`Baseline HTTP ${response.status}`);
                const baseline = await response.json();
                if (baseline.contextCapacity !== 131072 || baseline.dflash !== false || baseline.cpuMoeLayers !== 0)
                    throw new Error('Mac starting configuration mismatch');
                const records = new Map(baseline.records.map(record => [record.prompt, record]));
                const root = document.querySelector(`article[data-blog-post="${slug}"]`);
                return [...root.querySelectorAll('[data-blog-mac128-rate]')].map(node => {
                    const prompt = node.dataset.blogMac128Rate;
                    const expected = (1 / records.get(prompt).tpotSeconds).toFixed(2);
                    if (node.textContent.trim() !== expected) throw new Error(`Mac 128K ${prompt}: ${expected}`);
                    return {prompt, displayed: expected};
                });
            }""", {'url': urljoin(base, 'docs/benchmarks/qwen36-mac-128k-story-baseline.json'), 'slug': SLUG})
            assert len(report['mac128Metrics']) == 6
            report['checks'].append('All 6 Mac-only starting-point citations match the separate 128K summary')

            for width, name in ((1440, 'desktop'), (375, 'mobile')):
                page.set_viewport_size({'width': width, 'height': 1000 if width > 900 else 900})
                for language, title in TITLES.items():
                    scroll_top(page)
                    article.locator(f'[data-blog-language="{language}"]').click()
                    panel = article.locator(f'[data-blog-language-panel="{language}"]')
                    expect(panel).to_be_visible()
                    expect(article.locator('[data-blog-language-panel]:visible')).to_have_count(1)
                    expect(panel.locator('h1')).to_have_text(title)
                    expect(page).to_have_title(f'{title} — Prima Lab')
                    expect(article).to_have_attribute('lang', 'zh-CN' if language == 'zh' else 'en')
                    expect(article).to_have_attribute('aria-labelledby', panel.locator('h1').get_attribute('id'))
                    expect(article.locator(f'[data-blog-language="{language}"]')).to_have_attribute('aria-pressed', 'true')
                    text = panel.inner_text()
                    assert '262,144' in text and 'DFlash' in text and 'EOS' in text
                    assert '131,072' in text
                    assert not any(value in text for value in ('82.72', '83.38', '83.10'))
                    assert not any('128K' in paragraph and ('Windows' in paragraph or '台式机单机' in paragraph)
                                   for paragraph in panel.locator('p').all_text_contents())
                    assert ('All six mathematics answers' if language == 'en' else '六份数学答案全部有误') in text
                    assert ('not an exclusive ability to select 262K' if language == 'en' else '不是宣称只有 PRIMA 才能设置 262K') in text
                    assert ('not a claim that we processed a 262K-token document' if language == 'en' else '并不是已经输入了一篇 262K tokens 的文档') in text
                    scroll_top(page)
                    page.screenshot(path=str(args.artifacts / f'{name}-{language}-opening.png'))
                    figure = panel.locator('.blog-device-pair')
                    figure.scroll_into_view_if_needed()
                    for img in figure.locator('img').all():
                        img.scroll_into_view_if_needed()
                        img.evaluate('(img) => img.decode()')
                        assert img.get_attribute('alt')
                        assert img.evaluate('(img) => img.naturalWidth > 0')
                    figure.screenshot(path=str(args.artifacts / f'{name}-{language}-devices.png'))
                    table = panel.locator('.blog-table-wrap').first
                    table.scroll_into_view_if_needed()
                    table.screenshot(path=str(args.artifacts / f'{name}-{language}-results.png'))
                    layout = page.evaluate("""() => ({
                        document: document.documentElement.scrollWidth,
                        viewport: window.innerWidth,
                        main: document.querySelector('main').scrollWidth,
                        mainWidth: document.querySelector('main').clientWidth,
                    })""")
                    assert layout['document'] <= layout['viewport'] + 1, layout
                    assert layout['main'] <= layout['mainWidth'] + 1, layout
                    if width < 600:
                        assert table.evaluate('(node) => node.scrollWidth > node.clientWidth')
                        table.focus()
                        table.press('ArrowRight')
                        page.wait_for_function("""() => document.activeElement.matches('.blog-table-wrap')
                            && document.activeElement.scrollLeft > 0""")
                    report['checks'].append(f'{name}/{language}: title, language, source boundaries, images, tables, no overflow')

            page.set_viewport_size({'width': 1440, 'height': 1000})
            scroll_top(page)
            page.reload()
            page.wait_for_load_state('networkidle')
            expect(page).to_have_title(f'{TITLES["zh"]} — Prima Lab')
            expect(article).to_have_attribute('lang', 'zh-CN')
            article.locator('[data-blog-index-link]').click()
            expect(page.locator('[data-blog-index]')).to_be_visible()
            cards = page.locator('.blog-card')
            expect(cards).to_have_count(3)
            expect(cards.first).to_have_attribute('data-blog-post-link', SLUG)
            page.screenshot(path=str(args.artifacts / 'blog-index-desktop.png'))
            cards.first.click()
            expect(article).to_be_visible()
            page.go_back()
            expect(page.locator('[data-blog-index]')).to_be_visible()
            page.go_forward()
            expect(article).to_be_visible()
            report['checks'].append('New index card, direct route, reload/language persistence, back/forward history')

            article.locator('[data-blog-language-panel="zh"] [data-route-link="playground"]').click()
            expect(page.locator('[data-route-view="playground"]')).to_be_visible()
            page.locator('[data-sim-model]').select_option('qwen36-35b-a3b-iq1m')
            expect(page.locator('[data-sim-testbed]')).to_have_value('testbed-3')
            expect(page.locator('[data-sim-quality-note]')).to_be_visible()
            report['checks'].append('Article CTA reaches the original measured Testbed 3 playground')

            for old_slug in ('hunyuan4-770b-local-devices', 'workstation-already-in-room', 'inside-hy4-770b-experiment'):
                page.goto(urljoin(base, f'blog/{old_slug}/'))
                page.wait_for_load_state('networkidle')
                expect(page.locator(f'article[data-blog-post="{old_slug}"]')).to_be_visible()
                assert page.locator('article[data-blog-post]:visible').count() == 1
            report['checks'].append('All three existing article deep links still open')
            assert not report['pageErrors'], report['pageErrors']
            assert not report['siteHttpErrors'], report['siteHttpErrors']
            report['complete'] = True
        finally:
            (args.artifacts / 'blog-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
