"""Harmless GUI for verifying input into an elevated process."""
import ctypes, json, tkinter as tk
from pathlib import Path

root=tk.Tk(); root.title('Desktop Bridge admin input test'); root.geometry('680x280+250+180')
elevated=bool(ctypes.windll.shell32.IsUserAnAdmin())
tk.Label(root,text=f'Administrative GUI test — elevated: {elevated}',font=('Segoe UI',16)).pack(pady=20)
tk.Label(root,text='Type a test phrase and click Save test result. No system settings are changed.').pack()
entry=tk.Entry(root,font=('Segoe UI',14),width=45); entry.pack(pady=15); entry.focus_set()
message=tk.StringVar()
def save():
    Path(__file__).with_name('admin-test-result.json').write_text(json.dumps({'elevated':elevated,'text':entry.get()},ensure_ascii=False),encoding='utf-8')
    message.set('Test result saved')
tk.Button(root,text='Save test result',command=save).pack()
tk.Label(root,textvariable=message).pack(pady=10)
root.mainloop()
