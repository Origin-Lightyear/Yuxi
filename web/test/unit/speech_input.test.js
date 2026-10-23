import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SPEECH_MAX_RECORD_MS,
  decideTranscriptAction,
  isSpeechAvailable,
  nextRecorderState
} from '../../src/utils/speech_input.js'

test('非安全上下文不提供语音入口', () => {
  assert.equal(isSpeechAvailable({ secureContext: false, healthAvailable: true, disabled: false }), false)
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: true, disabled: false }), true)
})

test('识别服务不可用或输入框禁用时不提供入口', () => {
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: false, disabled: false }), false)
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: true, disabled: true }), false)
})

test('检测到语音后累计静音 800ms 停止', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: 3000, speechDetected: true, silentMs: 799, rms: 0.001 }),
    'continue'
  )
  assert.equal(
    nextRecorderState({ elapsedMs: 3000, speechDetected: true, silentMs: 800, rms: 0.001 }),
    'stop'
  )
})

test('起录 5 秒仍无语音则取消', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: 4999, speechDetected: false, silentMs: 0, rms: 0.001 }),
    'continue'
  )
  assert.equal(
    nextRecorderState({ elapsedMs: 5000, speechDetected: false, silentMs: 0, rms: 0.001 }),
    'cancel'
  )
})

test('超过 60 秒强制停止', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: SPEECH_MAX_RECORD_MS, speechDetected: true, silentMs: 0, rms: 0.5 }),
    'stop'
  )
})

test('输入框为空且会话未变才自动发送', () => {
  assert.equal(decideTranscriptAction({ draft: '', threadIdAtStart: 't1', currentThreadId: 't1' }), 'send')
  assert.equal(decideTranscriptAction({ draft: '我打了一半', threadIdAtStart: 't1', currentThreadId: 't1' }), 'append')
  assert.equal(decideTranscriptAction({ draft: '', threadIdAtStart: 't1', currentThreadId: 't2' }), 'discard')
})

// 会话切换优先于草稿非空：文本属于旧会话，不能追加到新会话
test('会话已切走时草稿非空也丢弃转写', () => {
  assert.equal(decideTranscriptAction({ draft: '打了一半', threadIdAtStart: 't1', currentThreadId: 't2' }), 'discard')
})
