/** 判定输入是否为本地 /help 指令：仅 trim 后完全等于 /help 才拦截，其余输入（含 /help extra、/HELP）原样走发送链路。 */
export function isHelpCommand(text) {
  return String(text ?? '').trim() === '/help'
}
