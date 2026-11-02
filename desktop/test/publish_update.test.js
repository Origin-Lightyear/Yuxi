const test = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { mkdtempSync, readdirSync, readFileSync, writeFileSync } = require('node:fs')
const { parseLatestYml, publishUpdate } = require('../scripts/publish-update.js')

const sha512 = (content) => crypto.createHash('sha512').update(content).digest('base64')

const latestYml = (version, artifact, content = 'installer', url = artifact) => `version: ${version}
files:
  - url: ${url}
    sha512: ${sha512(content)}
    size: ${content.length}
path: ${artifact}
sha512: ${sha512(content)}
releaseDate: 2026-11-02T00:00:00.000Z
`

const feedDir = () => mkdtempSync(path.join(os.tmpdir(), 'yuxi-feed-'))

function makeDist({ version = '0.1.0', artifact = '大力水手 Setup 0.1.0-x64.exe', content = 'installer', url } = {}) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'yuxi-update-'))
  writeFileSync(path.join(dir, 'latest.yml'), latestYml(version, artifact, content, url))
  writeFileSync(path.join(dir, artifact), content)
  writeFileSync(path.join(dir, `${artifact}.blockmap`), 'blockmap')
  return { dir, artifact }
}

test('解析 files 列表与 path', () => {
  assert.deepEqual(parseLatestYml(latestYml('0.1.0', 'a-x64.exe')), {
    version: '0.1.0',
    artifact: 'a-x64.exe',
    files: [{ url: 'a-x64.exe', sha512: sha512('installer') }],
  })
  assert.deepEqual(parseLatestYml(latestYml('0.1.0', '"a b-x64.exe"')).artifact, 'a b-x64.exe')
  assert.throws(() => parseLatestYml('version: 0.1.0\n'), /缺少 version 或 path/)
})

test('复制 latest.yml、安装包与差分包', () => {
  const { dir, artifact } = makeDist()
  const target = feedDir()
  const copied = publishUpdate({ distDir: dir, targetDir: target, packageVersion: '0.1.0' })
  assert.deepEqual([...copied].sort(), [artifact, 'latest.yml', `${artifact}.blockmap`].sort())
  assert.equal(readFileSync(path.join(target, artifact), 'utf8'), 'installer')
  assert.equal(readFileSync(path.join(target, `${artifact}.blockmap`), 'utf8'), 'blockmap')
  assert.equal(readdirSync(target).filter((name) => name.startsWith('.publish-')).length, 0)
})

test('没有差分包时只发布最新版与安装包', () => {
  const { dir, artifact } = makeDist()
  fs.rmSync(path.join(dir, `${artifact}.blockmap`))
  assert.deepEqual(publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }), [
    artifact,
    'latest.yml',
  ])
})

test('版本不一致时拒绝发布', () => {
  const { dir } = makeDist({ version: '0.0.9' })
  const target = feedDir()
  assert.throws(() => publishUpdate({ distDir: dir, targetDir: target, packageVersion: '0.1.0' }), /不一致/)
  assert.deepEqual(readdirSync(target), [])
})

test('sha512 不匹配时保留旧 latest.yml 并清理临时文件', () => {
  const { dir, artifact } = makeDist()
  writeFileSync(path.join(dir, artifact), 'tampered')
  const target = feedDir()
  writeFileSync(path.join(target, 'latest.yml'), 'old-latest')
  assert.throws(() => publishUpdate({ distDir: dir, targetDir: target, packageVersion: '0.1.0' }), /sha512/)
  assert.equal(readFileSync(path.join(target, 'latest.yml'), 'utf8'), 'old-latest')
  assert.deepEqual(readdirSync(target), ['latest.yml'])
})

test('安装包缺失时拒绝发布', () => {
  const { dir, artifact } = makeDist()
  fs.rmSync(path.join(dir, artifact))
  assert.throws(() => publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }), /不存在/)
})

test('path 不在 files 列表中时拒绝发布', () => {
  const { dir } = makeDist({ url: 'other-x64.exe' })
  assert.throws(() => publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }), /不在 files/)
})

test('文件名带目录时拒绝发布', () => {
  for (const url of ['../evil-x64.exe', '..\\evil-x64.exe']) {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'yuxi-update-'))
    writeFileSync(path.join(dir, 'latest.yml'), latestYml('0.1.0', url, 'installer', url))
    assert.throws(
      () => publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }),
      /不带目录/
    )
  }
})

test('非 x64 产物拒绝发布', () => {
  const { dir } = makeDist({ artifact: '大力水手 Setup 0.1.0-arm64.exe' })
  assert.throws(() => publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }), /x64/)
})

test('缺少 sha512 时拒绝发布', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'yuxi-update-'))
  writeFileSync(path.join(dir, 'latest.yml'), 'version: 0.1.0\nfiles:\n  - url: a-x64.exe\npath: a-x64.exe\n')
  writeFileSync(path.join(dir, 'a-x64.exe'), 'installer')
  assert.throws(
    () => publishUpdate({ distDir: dir, targetDir: feedDir(), packageVersion: '0.1.0' }),
    /sha512/
  )
})

test('目标目录已有同名但内容不同的安装包时拒绝发布', () => {
  const { dir, artifact } = makeDist()
  const target = feedDir()
  writeFileSync(path.join(target, artifact), 'old-content')
  assert.throws(
    () => publishUpdate({ distDir: dir, targetDir: target, packageVersion: '0.1.0' }),
    /提升版本号/
  )
  assert.equal(readFileSync(path.join(target, artifact), 'utf8'), 'old-content')
})
