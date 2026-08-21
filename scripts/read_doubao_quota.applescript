-- 只读：从已打开的豆包额度管理页返回最小可见文本。
-- 不导航、不点击，也不读取 Cookie、localStorage 或浏览器配置。

on run argv
  if (count of argv) is 0 then error "缺少豆包额度页 URL" number 1000
  set targetUrl to item 1 of argv
  set jsCode to "(() => { const lines = (document.body ? document.body.innerText : '').split(String.fromCharCode(10)).map(line => line.trim()).filter(Boolean); const current = lines.findIndex(line => line === '当前时段'); if (current < 0) throw new Error('quota section not found'); const record = lines.findIndex((line, index) => index > current && line === '订阅记录'); let management = -1; for (let index = 0; index < current; index += 1) { if (lines[index].includes('订阅与额度管理')) management = index; } const candidate = management > 0 ? lines[management - 1] : ''; const plan = candidate.endsWith('套餐') && !candidate.startsWith('升级至') && !candidate.startsWith('购买') ? candidate : ''; const managementLine = management >= 0 ? lines[management] : ''; return [plan, managementLine, ...lines.slice(current, record > current ? record + 1 : current + 8)].filter(Boolean).join(String.fromCharCode(10)); })()"

  tell application "Google Chrome"
    repeat with chromeWindow in windows
      repeat with chromeTab in tabs of chromeWindow
        set tabUrl to ""
        try
          set tabUrl to URL of chromeTab as text
        end try
        if tabUrl starts with targetUrl then
          set quotaText to execute chromeTab javascript jsCode
          if quotaText is missing value then error "豆包额度页 DOM 返回为空" number 1002
          return quotaText as text
        end if
      end repeat
    end repeat
  end tell

  error "未找到已打开的豆包额度管理页" number 1001
end run
