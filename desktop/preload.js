const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('yuxiDesktop', {
  submit: (value) => ipcRenderer.send('ask-url:submit', value),
});
