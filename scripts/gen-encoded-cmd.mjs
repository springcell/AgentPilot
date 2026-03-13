const script = `$ws = New-Object -ComObject WScript.Shell; $ws.Run('notepad'); Start-Sleep -Milliseconds 500; $ws.SendKeys('hahaha'); Start-Sleep -Milliseconds 200; $ws.SendKeys('^s'); Start-Sleep -Milliseconds 500; $ws.SendKeys($env:USERPROFILE + '\\Desktop\\hahaha.txt'); Start-Sleep -Milliseconds 200; $ws.SendKeys('~')`;
const buf = Buffer.from(script, 'utf16le');
console.log(buf.toString('base64'));
