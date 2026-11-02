/**
 * 把 electron-builder 的 Windows x64 更新产物复制到网页静态目录（/desktop-updates/）。
 *
 * latest.yml 是客户端的唯一事实源：只发布它 files 列表声明的文件，校验 sha512 后
 * 原子改名，最后才替换 latest.yml。任一步失败都保留旧版本，客户端不会看到半包。
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const LATEST_FILE = 'latest.yml';
const X64_ARTIFACT = /-x64\.exe$/i;

function unquote(value) {
  return value.replace(/^['"]|['"]$/g, '');
}

function parseLatestYml(text) {
  const top = {};
  const files = [];
  let inFiles = false;
  let current = null;

  for (const line of text.split(/\r?\n/)) {
    // 顶层键（version/path/files）顶格；其余是 files 列表的缩进字段
    if (/^\S/.test(line)) {
      inFiles = false;
      current = null;
      const topLevel = /^([a-z0-9]+):\s*(.*)$/i.exec(line);
      if (!topLevel) continue;
      if (topLevel[1] === 'files') {
        inFiles = true;
      } else {
        top[topLevel[1]] = unquote(topLevel[2]);
      }
      continue;
    }
    if (!inFiles) continue;
    const item = /^\s*-\s+([a-z0-9]+):\s*(.*)$/i.exec(line);
    if (item) {
      current = { [item[1]]: unquote(item[2]) };
      files.push(current);
      continue;
    }
    const field = /^\s*([a-z0-9]+):\s*(.*)$/i.exec(line);
    if (field && current) current[field[1]] = unquote(field[2]);
  }

  if (!top.version || !top.path) throw new Error('latest.yml 缺少 version 或 path');
  const entries = files
    .filter((file) => file.url)
    .map((file) => ({ url: file.url, sha512: file.sha512 || null }));
  if (entries.length === 0) entries.push({ url: top.path, sha512: null });
  return { version: top.version, artifact: top.path, files: entries };
}

function sha512Base64(filePath) {
  return crypto.createHash('sha512').update(fs.readFileSync(filePath)).digest('base64');
}

function assertSafeNames(latest) {
  if (!latest.files.some((file) => file.url === latest.artifact)) {
    throw new Error(`latest.yml 的 path 不在 files 列表中：${latest.artifact}`);
  }
  for (const file of latest.files) {
    if (path.basename(file.url) !== file.url || path.win32.basename(file.url) !== file.url) {
      throw new Error(`latest.yml 的更新文件名必须是不带目录的文件名：${file.url}`);
    }
  }
}

function publishUpdate({ distDir, targetDir, packageVersion }) {
  const latestPath = path.join(distDir, LATEST_FILE);
  if (!fs.existsSync(latestPath)) throw new Error(`缺少 ${latestPath}，请先运行 Windows 打包`);
  const latestText = fs.readFileSync(latestPath, 'utf8');
  const latest = parseLatestYml(latestText);
  if (latest.version !== packageVersion) {
    throw new Error(`latest.yml 版本 ${latest.version} 与 package.json ${packageVersion} 不一致，拒绝发布陈旧产物`);
  }
  if (!X64_ARTIFACT.test(latest.artifact)) {
    throw new Error(`自动更新只发布 x64 安装包：${latest.artifact}`);
  }
  assertSafeNames(latest);
  if (latest.files.some((file) => !file.sha512)) {
    throw new Error('latest.yml 的 files 缺少 sha512，拒绝发布无法校验的产物');
  }

  // 同版本重发时同名安装包会被覆盖；旧 latest.yml 可能仍指向旧内容，必须先提升版本号
  const artifactEntry = latest.files.find((file) => file.url === latest.artifact);
  const existingArtifact = path.join(targetDir, latest.artifact);
  if (fs.existsSync(existingArtifact) && sha512Base64(existingArtifact) !== artifactEntry.sha512) {
    throw new Error(`目标目录已有同名但内容不同的安装包，请提升版本号：${latest.artifact}`);
  }

  // 先复制到目标目录内的临时目录，校验通过再改名；latest.yml 最后替换
  fs.mkdirSync(targetDir, { recursive: true });
  const tempDir = fs.mkdtempSync(path.join(targetDir, '.publish-'));
  const blockmapName = `${latest.artifact}.blockmap`;
  const blockmapSource = path.join(distDir, blockmapName);
  const hasBlockmap = fs.existsSync(blockmapSource);
  if (!hasBlockmap) console.warn(`缺少差分包 ${blockmapSource}，客户端将整包下载`);

  try {
    for (const file of latest.files) {
      const source = path.join(distDir, file.url);
      if (!fs.existsSync(source)) throw new Error(`latest.yml 指向的安装包不存在：${source}`);
      const copy = path.join(tempDir, file.url);
      fs.copyFileSync(source, copy);
      if (file.sha512 && sha512Base64(copy) !== file.sha512) {
        throw new Error(`sha512 校验失败：${file.url}`);
      }
    }
    if (hasBlockmap) fs.copyFileSync(blockmapSource, path.join(tempDir, blockmapName));

    // 安装包先就位，latest.yml 最后生效；同版本重发时客户端只会在极短窗口内看到旧哈希
    for (const file of latest.files) {
      fs.renameSync(path.join(tempDir, file.url), path.join(targetDir, file.url));
    }
    if (hasBlockmap) fs.renameSync(path.join(tempDir, blockmapName), path.join(targetDir, blockmapName));
    fs.writeFileSync(path.join(tempDir, LATEST_FILE), latestText);
    fs.renameSync(path.join(tempDir, LATEST_FILE), path.join(targetDir, LATEST_FILE));
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }

  return [...latest.files.map((file) => file.url), LATEST_FILE, hasBlockmap ? blockmapName : null].filter(Boolean);
}

if (require.main === module) {
  const root = path.join(__dirname, '..');
  const { version } = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const copied = publishUpdate({
    distDir: path.join(root, 'dist'),
    targetDir: path.join(root, '..', 'web', 'public', 'desktop-updates'),
    packageVersion: version,
  });
  console.log(`已发布 v${version}：${copied.join('、')}`);
}

module.exports = { parseLatestYml, publishUpdate };
