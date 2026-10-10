const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('yuxiDesktop', {
  submit: (value) => ipcRenderer.send('ask-url:submit', value),
  // 隐藏窗口 focus() 无效，通知点击需主进程 restore/show/focus
  showMainWindow: () => ipcRenderer.send('yuxi:show-main-window'),
});
