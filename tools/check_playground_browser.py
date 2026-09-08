#!/usr/bin/env python3
"""Explicit native Chromium integration check; requires the Playwright environment."""
import argparse
import bisect
import json
import math
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:65300/playground/')
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'pageErrors': [], 'localHttpErrors': []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True, args=['--disable-gpu'])
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            page = context.new_page()
            page.on('pageerror', lambda error: report['pageErrors'].append(str(error)))
            page.on('response', lambda response: report['localHttpErrors'].append(response.url)
                    if response.status >= 400 and response.url.startswith('http://127.0.0.1:') else None)
            page.clock.install(time='2026-09-08T00:00:00Z')
            page.goto(args.url)
            page.wait_for_load_state('networkidle')
            page.clock.pause_at('2026-09-08T00:05:00Z')
            # Reconnaissance before interaction, including the original case.
            page.screenshot(path=str(args.artifacts / 'initial-desktop.png'), full_page=True)
            assert page.locator('[data-sim-model]').count() == 1
            assert page.locator('[data-sim-result]').count() == 3
            dataset = page.evaluate("async () => (await import('./src/qwen36-replay-data-20260908.js')).qwen36ReplayData")
            assert len(dataset['prompts']) == 3, 'Measured dataset not yet complete'
            select_model = page.locator('[data-sim-model]')
            select_model.select_option('qwen36-35b-a3b-iq1m')
            assert page.locator('[data-sim-testbed]').input_value() == 'testbed-3'
            assert page.locator('[data-testbed-field="homeName"]').inner_text() == 'MacBook Pro M3'
            assert page.locator('[data-testbed-field="serverName"]').inner_text() == 'Windows desktop'
            assert page.locator('[data-sim-home-image]').get_attribute('src').endswith('/assets/macbook-pro-m3.png')
            assert page.locator('[data-sim-wan-image]').is_hidden()
            assert page.locator('[data-sim-local-network]').is_visible()
            assert page.locator('[data-sim-dflash-label]').inner_text() == 'DFlash'
            assert page.locator('[data-sim-quality-note]').is_visible()
            assert 'not an accuracy benchmark' in page.locator('[data-sim-quality-note]').inner_text()
            assert 'Windows desktop' in page.locator('#simulation-purpose-detail').inner_text()
            for prompt_id, prompt in dataset['prompts'].items():
                assert page.locator(f'[data-sim-prompt] option[value="{prompt_id}"]').text_content() == prompt['prompt']
            report['checks'].append('Testbed 3 hardware, illustrations, LAN, DFlash label, exact prompt strings')
            page.screenshot(path=str(args.artifacts / 'testbed3-desktop-idle.png'), full_page=True)

            for prompt_id, prompt in dataset['prompts'].items():
                for mode in ('off', 'on'):
                    page.locator('[data-sim-prompt]').select_option(prompt_id)
                    checkbox = page.locator('[data-sim-dflash]')
                    if checkbox.is_checked() != (mode == 'on'):
                        page.locator('[data-sim-dflash-control]').click(force=True)
                    assert checkbox.is_checked() == (mode == 'on')
                    measurements = prompt['modes'][mode]['results']
                    for result_id, measurement in measurements.items():
                        panel = page.locator(f'[data-sim-result-id="{result_id}"]')
                        assert panel.locator('[data-sim-result-rate-output]').inner_text() == f'{1 / measurement["tpotSeconds"]:,.1f} tok/s'
                        assert panel.locator('[data-sim-result-ttft]').inner_text() == f'{measurement["ttftSeconds"]:.2f} s'
                    page.locator('[data-sim-send]').click(force=True)
                    assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'streaming'
                    # Reduced-motion preference must not bypass measured TTFT.
                    assert all(page.locator(f'[data-sim-result-id="{key}"] [data-sim-result-token-count]').inner_text() == '0'
                               for key in measurements)
                    current = 0
                    earliest = min(value['ttftSeconds'] * 1000 for value in measurements.values())
                    before = max(0, math.floor(earliest) - 35)
                    page.clock.run_for(before)
                    current = before
                    assert all(page.locator(f'[data-sim-result-id="{key}"] [data-sim-result-token-count]').inner_text() == '0'
                               for key in measurements)
                    # Check several real timestamp boundaries, including bursts.
                    points = {math.ceil(value['events'][0][0] * 1000) + 40 for value in measurements.values()}
                    points.update(math.ceil(value['events'][min(10, len(value['events']) - 1)][0] * 1000) + 40
                                  for value in measurements.values())
                    for target in sorted(points):
                        page.clock.run_for(max(0, target - current))
                        current = max(current, target)
                        for result_id, measurement in measurements.items():
                            arrivals = [event[0] * 1000 for event in measurement['events']]
                            actual = int(page.locator(f'[data-sim-result-id="{result_id}"] [data-sim-result-token-count]').inner_text())
                            # Rendering occurs on RAF; permit one frame of delay,
                            # never advance ahead of the captured timeline.
                            assert bisect.bisect_right(arrivals, current - 18) <= actual <= bisect.bisect_right(arrivals, current + 1), (prompt_id, mode, result_id, current, actual)
                    last = max(value['events'][-1][0] * 1000 for value in measurements.values())
                    page.clock.fast_forward(max(0, math.ceil(last) + 100 - current))
                    page.clock.run_for(32)
                    assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'complete'
                    for result_id, measurement in measurements.items():
                        panel = page.locator(f'[data-sim-result-id="{result_id}"]')
                        assert int(panel.locator('[data-sim-result-token-count]').inner_text()) == measurement['tokenCount']
                        equal = page.evaluate("""async ({id, text}) => {
                            const {renderMarkdownInto} = await import('./src/markdown-renderer.js');
                            const expected = document.createElement('div');
                            renderMarkdownInto(expected, text);
                            return document.querySelector(`[data-sim-result-id="${id}"] [data-sim-result-output]`).innerHTML === expected.innerHTML;
                        }""", {'id': result_id, 'text': measurement['output']})
                        assert equal, 'Final rendered output differs from the recorded text'
                    report['checks'].append(f'{prompt_id}/{mode}: 3 real rates + TTFT + arrivals + complete unedited outputs')
                    if prompt_id == 'cs' and mode == 'on':
                        page.screenshot(path=str(args.artifacts / 'testbed3-code-complete.png'), full_page=True)

            # Stop/reset while waiting, before the first visible token.
            page.locator('[data-sim-prompt]').select_option('math')
            page.locator('[data-sim-send]').click(force=True)
            page.clock.run_for(32)
            page.locator('[data-sim-send]').click(force=True)
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'stopped'
            page.clock.fast_forward(60000)
            assert page.locator('[data-sim-result-id="home"] [data-sim-result-token-count]').inner_text() == '0'
            page.locator('[data-sim-reset]').evaluate('(button) => button.click()')
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'idle'
            report['checks'].append('Stop/reset cancels pending first-token playback')

            # Pause/resume and page visibility must freeze request time, not
            # skip a minute of recorded output when playback resumes.
            page.locator('[data-sim-send]').click(force=True)
            page.clock.run_for(1500)
            counts = lambda: page.locator('[data-sim-result-token-count]').all_text_contents()
            page.locator('[data-sim-pause]').evaluate('(button) => button.click()')
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'paused'
            paused_counts = counts()
            page.clock.fast_forward(60000)
            assert counts() == paused_counts
            page.locator('[data-sim-pause]').evaluate('(button) => button.click()')
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'streaming'
            page.clock.run_for(200)
            assert counts() != paused_counts
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'suspended'
            suspended_counts = counts()
            page.clock.fast_forward(60000)
            assert counts() == suspended_counts
            page.evaluate("""() => {
                delete document.hidden;
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'streaming'
            page.clock.run_for(200)
            assert counts() != suspended_counts
            page.locator('[data-sim-send]').click(force=True)
            page.locator('[data-sim-reset]').evaluate('(button) => button.click()')
            report['checks'].append('Pause/resume and hidden-tab suspension preserve recorded request time')

            # In-memory fault injection only: no synthetic or modified replay
            # is ever written to production data. Fail closed for a missing
            # host measurement and for a missing entire mode.
            for missing in ('host', 'mode'):
                page.evaluate("""async (missing) => {
                    const {qwen36ReplayData: data} = await import('./src/qwen36-replay-data-20260908.js');
                    window.__testReplayRestore = structuredClone(data.prompts.math.modes.on);
                    if (missing === 'host') delete data.prompts.math.modes.on.results.prima;
                    else delete data.prompts.math.modes.on;
                    document.querySelector('[data-sim-prompt]').dispatchEvent(new Event('change'));
                }""", missing)
                assert page.locator('[data-simulator]').get_attribute('data-sim-state') == 'unavailable'
                assert page.locator('[data-sim-send]').is_disabled()
                assert page.locator('[data-sim-result-rate-output]').all_text_contents() == ['—', '—', '—']
                page.clock.fast_forward(60000)
                assert counts() == ['0', '0', '0']
                page.evaluate("""async () => {
                    const {qwen36ReplayData: data} = await import('./src/qwen36-replay-data-20260908.js');
                    data.prompts.math.modes.on = window.__testReplayRestore;
                    delete window.__testReplayRestore;
                    document.querySelector('[data-sim-prompt]').dispatchEvent(new Event('change'));
                }""")
                assert page.locator('[data-sim-send]').is_enabled()
            report['checks'].append('Missing host/mode fails closed: no fallback rates, outputs or playback')

            # Existing examples retain original hardware, labels and rates.
            select_model.select_option('qwen38-27b-q8')
            assert page.locator('[data-testbed-field="homeName"]').inner_text() == 'Home Laptop'
            assert page.locator('[data-testbed-field="serverName"]').inner_text() == 'GPU server'
            assert page.locator('[data-sim-dflash-label]').inner_text() == 'DFlash2'
            assert page.locator('[data-sim-wan-image]').is_visible()
            assert page.locator('[data-sim-local-network]').is_hidden()
            assert page.locator('[data-sim-quality-note]').is_hidden()
            assert 'GPU server only' in page.locator('#simulation-purpose-detail').inner_text()
            assert page.locator('[data-sim-result-id="prima"] [data-sim-result-rate-output]').inner_text() == '15.2 tok/s'
            assert page.locator('[data-sim-result-ttft]').first.is_hidden()
            select_model.select_option('hy4-770b-stq1-0')
            assert page.locator('[data-sim-testbed]').input_value() == 'testbed-1'
            assert page.locator('[data-sim-dflash-control]').is_hidden()
            assert 'A4000 x4' in page.locator('[data-sim-server-gpu]').inner_text()
            report['checks'].append('Qwen3.8 and Hy4 regressions: original devices, assets, rates and mode controls')

            select_model.select_option('qwen36-35b-a3b-iq1m')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.clock.run_for(100)
            page.screenshot(path=str(args.artifacts / 'testbed3-mobile-idle.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), 'Horizontal overflow on mobile'
            report['checks'].append('390px mobile layout without horizontal overflow')
            assert not report['pageErrors'], report['pageErrors']
            assert not report['localHttpErrors'], report['localHttpErrors']
            report['complete'] = True
        finally:
            browser.close()
            (args.artifacts / 'browser-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
