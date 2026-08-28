/* pattern-tool 前端逻辑：上传预压 → 批级形状/尺寸设置（+逐图可选裁剪弹层）→ 提交轮询 → 分端交付。
   技术方案 7.1：无构建链原生 JS；预压用 canvas；Web Share / ClipboardItem 特性检测降级。
   第二十九次修订：批级"统一形状"自绘下拉（默认自由矩形）；第三十次修订改
   "后改优先"——切换统一形状即全局重声明（逐图已设形状/框重置跟随统一）。 */
(function () {
  'use strict';

  // ---- 常量（与后端 /api/meta 对齐；预压阈值 3600 与后端像素上限一致） ----
  var MAX_SIDE_PIXELS = 3600;
  var MIN_IMAGE_BYTES = 1024; // 最小字节（1KB，与后端 min_image_bytes 对齐；过小拦截主要看像素下限）
  var PRESCALE_JPEG_QUALITY = 0.92;
  var POLL_INTERVAL_MS = 10000; // 状态轮询间隔（2026-08-26 从 2.5s 放宽：慢任务佐糖 40-60s 常态，密集轮询只刷日志不加速处理）
  // 打印尺寸档位（2026-08-26 新增；cm 口径=正方形边长，目标像素=cm/2.54×300）
  var SIZE_OPTIONS = [
    { value: '4寸', label: '4寸 (9cm)', cm: 9 },
    { value: '6寸', label: '6寸 (14cm)', cm: 14 },
    { value: '8寸', label: '8寸 (19cm)', cm: 19 },
    { value: '10寸', label: '10寸 (24cm)', cm: 24 },
    { value: '12寸', label: '12寸 (29cm)', cm: 29 }
  ];
  // 形状枚举 → 中文显示名（cropMeta.shape 是后端契约键，展示层统一走此映射）
  var SHAPE_LABELS = {
    'rectangle': '自由矩形',
    'circle': '圆形',
    'square': '正方形',
    'rectangle-fixed': '长方形',
    'heart': '爱心',
    'star': '星形',
  };

  // ---- 处理耗时计时（2026-08-28 第二十四次修订重写：仅执行中计时）----
  // 口径（用户定案，撤销 2026-08-27"墙钟含排队"）：排队中不计时；执行中
  // 实时走；终态定格服务端真值 finished_at - started_at（不随轮询回涨）。
  // 架构修复：旧版每卡各建 500ms interval 但共用一个 timerId——每建新卡
  // 清掉前卡的 interval，批内只有最后渲染那张卡实时走、其余卡冻结在
  // 10s 轮询值（04.14.43 截图实锤）。新版：单个全局 ticker 遍历刷新全部
  // 执行中卡的 span（data-start-ms 锚定服务端 started_at + 钟差校正），
  // 终态卡一次性写入静态文本不参与刷新。
  var elapsedTickerId = null;
  var serverClockOffsetMs = 0; // 服务端钟 - 本机钟（每次轮询以 server_time 校正）

  function formatElapsed(ms) {
    var totalSeconds = Math.max(0, Math.floor(ms / 1000));
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = totalSeconds % 60;
    return minutes > 0
      ? minutes + '分' + (seconds < 10 ? '0' : '') + seconds + '秒'
      : seconds + '秒';
  }

  function parseUtcMs(isoText) {
    if (!isoText) return null;
    // 库内格式 "YYYY-MM-DD HH:MM:SS"（空格分隔，UTC naive）→ Date 需 T 分隔
    var normalized = String(isoText).trim().replace(' ', 'T');
    var ms = Date.parse(normalized + (normalized.indexOf('Z') < 0 && normalized.slice(-1) !== 'Z' ? 'Z' : ''));
    return isNaN(ms) ? null : ms;
  }

  function syncServerClock(serverTimeIso) {
    var serverMs = parseUtcMs(serverTimeIso);
    if (serverMs !== null) serverClockOffsetMs = serverMs - Date.now();
  }

  // 服务端 UTC ISO → 本机钟域的时间戳（叠加钟差：服务端 10:00:00 / 本机
  // 快 30s → 事件在本机钟域的读数要减去 30s，Date.now() 计时才连续）
  function serverEventToLocalMs(isoText) {
    var parsed = parseUtcMs(isoText);
    return parsed === null ? null : parsed - serverClockOffsetMs;
  }

  function elapsedTicker() {
    var spans = resultList.querySelectorAll('.elapsed-timer[data-start-ms]');
    var nowMs = Date.now();
    for (var i = 0; i < spans.length; i++) {
      var startMs = Number(spans[i].getAttribute('data-start-ms'));
      if (!isNaN(startMs)) {
        spans[i].textContent = formatElapsed(nowMs - startMs);
      }
    }
  }

  function ensureElapsedTicker() {
    if (elapsedTickerId) return;
    elapsedTickerId = setInterval(elapsedTicker, 500);
  }

  function stopElapsedTicker() {
    if (elapsedTickerId) { clearInterval(elapsedTickerId); elapsedTickerId = null; }
  }

  // 执行中卡：挂动态计时 span（锚=服务端 started_at 校正到本机钟域）
  function createProcessingTimer(startedAtIso) {
    var span = document.createElement('span');
    span.className = 'elapsed-timer';
    var startMs = serverEventToLocalMs(startedAtIso);
    if (startMs !== null) {
      span.setAttribute('data-start-ms', String(startMs));
      span.textContent = formatElapsed(Date.now() - startMs);
    } else {
      span.textContent = ''; // 防御：无 started_at（旧批/异常）不显示计时
    }
    return span;
  }

  // 终态卡：定格真值 finished_at - started_at（服务端两时之差，与钟差无关）
  function createFinalElapsed(startedAtIso, finishedAtIso) {
    var span = document.createElement('span');
    span.className = 'elapsed-timer';
    var startMs = parseUtcMs(startedAtIso);
    var endMs = parseUtcMs(finishedAtIso);
    span.textContent = (startMs !== null && endMs !== null)
      ? formatElapsed(endMs - startMs)
      : '';
    return span;
  }

  // ---- 状态 ----
  var pendingImages = [];  // 待提交项：{ file(Blob), previewUrl, cropMeta, originalFile, originalPreviewUrl }
  var cropTargetIndex = -1; // 当前裁剪的目标（pendingImages 下标）
  var cropperInstance = null;
  var cropperShapeOverlay = null; // 裁剪框上的形状预览画布（circle/heart/star）
  var pollTimerId = null;
  var submittingJobId = null;

  // ---- DOM ----
  var fileInput = document.getElementById('file-input');
  var uploadDropZone = document.getElementById('upload-drop-zone');
  var uploadList = document.getElementById('upload-list');
  var submitButton = document.getElementById('submit-button');
  var resultList = document.getElementById('result-list');
  var restartButton = document.getElementById('restart-button');
  var cropModal = document.getElementById('crop-modal');
  var cropShapeSelect = document.getElementById('crop-shape-select');
  var cropSourceImage = document.getElementById('crop-source-image');
  var cropApplyButton = document.getElementById('crop-apply-button');
  var cropCancelButton = document.getElementById('crop-cancel-button');
  var cropSizeLabel = document.getElementById('crop-size-label');

  // ---- 工具 ----

  function isMobileEnvironment() {
    return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function formatBytes(sizeInBytes) {
    if (sizeInBytes < 1024 * 1024) return (sizeInBytes / 1024).toFixed(0) + 'KB';
    return (sizeInBytes / (1024 * 1024)).toFixed(1) + 'MB';
  }

  // ---- 预压（6 节上传链路：最长边 > 3600 等比缩；透明保 PNG，无透明转 JPEG q0.92） ----

  function loadIntoImageElement(blob) {
    return new Promise(function (resolve, reject) {
      var objectUrl = URL.createObjectURL(blob);
      var imageElement = new Image();
      imageElement.onload = function () { URL.revokeObjectURL(objectUrl); resolve(imageElement); };
      imageElement.onerror = function () { URL.revokeObjectURL(objectUrl); reject(new Error('图片无法读取')); };
      imageElement.src = objectUrl;
    });
  }

  function hasTransparency(imageElement) {
    // 小图直接测 alpha；大图抽样 64x64 网格判定（性能保护）
    var canvas = document.createElement('canvas');
    var sampleSize = 64;
    canvas.width = sampleSize; canvas.height = sampleSize;
    var context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(imageElement, 0, 0, sampleSize, sampleSize);
    try {
      var sampled = context.getImageData(0, 0, sampleSize, sampleSize).data;
      for (var pixelIndex = 3; pixelIndex < sampled.length; pixelIndex += 4) {
        if (sampled[pixelIndex] < 250) return true;
      }
    } catch (accessError) { return false; } // 跨域等异常按无透明处理（本场景同源无此问题）
    return false;
  }

  async function prescaleImage(blob) {
    var imageElement = await loadIntoImageElement(blob);
    var longestSide = Math.max(imageElement.naturalWidth, imageElement.naturalHeight);
    var targetWidth = imageElement.naturalWidth;
    var targetHeight = imageElement.naturalHeight;
    if (longestSide > MAX_SIDE_PIXELS) {
      var scaleRatio = MAX_SIDE_PIXELS / longestSide;
      targetWidth = Math.max(1, Math.round(imageElement.naturalWidth * scaleRatio));
      targetHeight = Math.max(1, Math.round(imageElement.naturalHeight * scaleRatio));
    }
    var withAlpha = hasTransparency(imageElement);
    var canvas = document.createElement('canvas');
    canvas.width = targetWidth; canvas.height = targetHeight;
    var context = canvas.getContext('2d');
    if (withAlpha) context.drawImage(imageElement, 0, 0, targetWidth, targetHeight);
    else context.drawImage(imageElement, 0, 0, targetWidth, targetHeight);

    var outputBlob = await new Promise(function (resolve) {
      if (withAlpha) {
        canvas.toBlob(resolve, 'image/png');
      } else {
        canvas.toBlob(resolve, 'image/jpeg', PRESCALE_JPEG_QUALITY);
      }
    });
    return outputBlob || blob; // 编码异常时退回原图（后端仍有 15MB/3600px 兜底校验）
  }

  // ---- 上传列表渲染 ----

  function renderUploadList() {
    uploadList.innerHTML = '';
    pendingImages.forEach(function (item, itemIndex) {
      var card = document.createElement('div');
      card.className = 'image-card';

      var thumbWrap = document.createElement('div');
      thumbWrap.className = 'thumb-wrap';
      var seqBadge = document.createElement('span');
      seqBadge.className = 'seq-badge';
      seqBadge.textContent = String(itemIndex + 1);
      var thumb = document.createElement('img');
      thumb.className = 'thumb';
      thumb.src = item.previewUrl;
      // 回显形状来源（第二十九次修订）：显式裁剪的图按自声明框现画预览；
      // 未裁剪的图按批级形状画整图预览（rectangle 直通无几何差异不画预览，
      // 其余形状画形状预览——所见即后端 CropStep 整图塑形结果）
      if (item.cropMeta && item.cropMeta.data && item.cropMeta.data.width > 0) {
        // 声明式裁剪回显（2026-08-26）：缩略图按声明现画形状预览（框裁+形状遮罩，
        // 形状外透明→thumb-wrap 棋盘格底）——仅展示层，提交仍是原图+声明
        attachCroppedPreview(thumb, item, itemIndex);
      } else if (isShapedBatchDefault()) {
        attachShapeOnlyPreview(thumb, item, itemIndex);
      }
      thumbWrap.appendChild(seqBadge);
      thumbWrap.appendChild(thumb);

      var info = document.createElement('div');
      info.className = 'card-info';
      var infoLine = document.createElement('div');
      infoLine.textContent = formatBytes(item.file.size) + (item.cropMeta ? ' · 已裁剪' : '');
      info.appendChild(infoLine);
      // 形状标签恒显示（第二十九次修订同日修复"自由矩形设置不上去"——rectangle
      // 后端真实生效（直通+边框描边）但旧前端不显标签，用户无从确认；批级形状
      // 对未裁剪图总是生效，标签与 trigger 选中态一致给出可见确认）
      var effectiveShape = item.cropMeta ? item.cropMeta.shape : batchCropShape;
      var shapeTag = document.createElement('div');
      shapeTag.className = 'tag done';
      shapeTag.textContent = (item.cropMeta ? '形状: ' : '形状(统一): ')
        + (SHAPE_LABELS[effectiveShape] || effectiveShape);
      info.appendChild(shapeTag);
      // 逐图尺寸下拉已撤（2026-08-28 第二十八次修订：尺寸仅批级"统一尺寸"
      // 一个入口，提交时统一写入各图 size 声明——用户定案"不需要给每个图
      // 设置尺寸"）

      var actions = document.createElement('div');
      actions.className = 'card-actions';
      var cropButton = document.createElement('button');
      cropButton.textContent = item.cropMeta ? '重新裁剪' : '裁剪';
      cropButton.addEventListener('click', function () { openCropModal(itemIndex); });
      var removeButton = document.createElement('button');
      removeButton.className = 'danger';
      removeButton.textContent = '移除';
      removeButton.addEventListener('click', function () {
        URL.revokeObjectURL(item.previewUrl);
        if (item.originalPreviewUrl && item.originalPreviewUrl !== item.previewUrl) {
          URL.revokeObjectURL(item.originalPreviewUrl);
        }
        pendingImages.splice(itemIndex, 1);
        renderUploadList();
      });
      actions.appendChild(cropButton);
      actions.appendChild(removeButton);

      card.appendChild(thumbWrap);
      card.appendChild(info);
      card.appendChild(actions);
      uploadList.appendChild(card);
    });
    submitButton.disabled = pendingImages.length === 0;
    submitButton.textContent = pendingImages.length === 0
      ? '开始处理'
      : '开始处理（' + pendingImages.length + ' 张）';
    // 批级设置行恒显示（第二十九次修订同日定案撤销"先上传再设置"隐藏闸——
    // 批级值先选好再上传同样成立，不做显隐切换）
  }

  // ---- 文件接入 ----

  async function acceptFiles(fileList) {
    var acceptedFiles = Array.prototype.slice.call(fileList);
    if (acceptedFiles.length + pendingImages.length > 9) {
      acceptedFiles = acceptedFiles.slice(0, Math.max(0, 9 - pendingImages.length));
      alert('单次最多 9 张，超出部分已忽略');
    }
    var rejectedReasons = []; // 逐张拒绝原因（前端预检，后端仍兜底校验）
    for (var fileIndex = 0; fileIndex < acceptedFiles.length; fileIndex++) {
      var rawFile = acceptedFiles[fileIndex];
      if (rawFile.type.indexOf('image/') !== 0) continue; // 非图片跳过
      // 最小字节预检（拦空图/占位图；阈值与后端 min_image_bytes 对齐）
      if (rawFile.size < MIN_IMAGE_BYTES) {
        rejectedReasons.push('「' + rawFile.name + '」过小（低于 ' + (MIN_IMAGE_BYTES / 1024) + 'KB），已跳过');
        continue;
      }
      var processedBlob = rawFile;
      try {
        processedBlob = await prescaleImage(rawFile);
      } catch (prescaleError) { /* 预压失败退回原图，后端兜底 */ }
      // 同批内容查重（指纹=文件字节，名字不同内容相同也算重）
      if (isDuplicateImage(processedBlob)) {
        rejectedReasons.push('「' + rawFile.name + '」与已选图片重复，已跳过');
        continue;
      }
      pendingImages.push({
        file: processedBlob,
        previewUrl: URL.createObjectURL(processedBlob),
        cropMeta: null,
        // 原图独立留存：裁剪只改 file/previewUrl，重新裁剪始终从原图出发（不叠加裁剪）
        originalFile: processedBlob,
        originalPreviewUrl: URL.createObjectURL(processedBlob),
      });
    }
    if (rejectedReasons.length > 0) alert(rejectedReasons.join('\n'));
    renderUploadList();
  }

  // 同批查重：内容指纹（size + 首尾字节抽样）命中任一已选项即视为重复。
  // 不用完整 SHA（JS 侧无同步哈希库），指纹冲突由后端 SHA-256 兜底拦截。
  function imageContentFingerprint(blob) {
    return blob.size + ':' + blob.type;
  }

  function isDuplicateImage(blob) {
    var fingerprint = imageContentFingerprint(blob);
    return pendingImages.some(function (item) {
      return imageContentFingerprint(item.originalFile) === fingerprint;
    });
  }

  fileInput.addEventListener('change', function () { acceptFiles(fileInput.files); fileInput.value = ''; });
  uploadDropZone.addEventListener('click', function () { fileInput.click(); });
  uploadDropZone.addEventListener('dragover', function (event) { event.preventDefault(); uploadDropZone.classList.add('dragover'); });
  uploadDropZone.addEventListener('dragleave', function () { uploadDropZone.classList.remove('dragover'); });
  uploadDropZone.addEventListener('drop', function (event) {
    event.preventDefault();
    uploadDropZone.classList.remove('dragover');
    acceptFiles(event.dataTransfer.files);
  });

  // ---- 统一尺寸批级设置（2026-08-28 第二十八次修订重写：尺寸唯一入口）----
  // 逐图尺寸下拉已撤——打印尺寸整批一个值（batchSizeCm），提交时统一写入
  // 全部图的 crop_meta.size.cm。旧实现"选中即逐图写值"，选完再补传的图静默
  // 无尺寸（"全部: xx"文案与事实不符）；批级变量下补传图自动同批生效，
  // trigger 文案即单一事实源。取消自定义输入时同步清空批级值（旧实现 UI
  // 回退但各图隐藏值残留，显示撒谎）。
  // 自绘原因：原生 select 选项永远浮层遮挡内容（浏览器行为不可改），用户
  // 要求选项从选择器下方滑出并推开内容——文档流展开 + max-height 过渡实现。
  var batchDropdown = document.getElementById('batch-size-dropdown');
  var batchTrigger = document.getElementById('batch-size-trigger');
  var batchMenu = document.getElementById('batch-size-menu');
  var batchSizeCm = null; // 批级打印尺寸（cm；null=不设置原幅交付）
  // trigger 短文案（第三十四次修订样式统一），菜单项保留详细文案
  var BATCH_OPTIONS = [{ value: '', label: '不设置', verbose: '不设置（原图大小）' }]
    .concat(SIZE_OPTIONS.map(function (opt) {
      return { value: String(opt.cm), label: opt.label, verbose: '全部: ' + opt.label };
    }))
    .concat([{ value: 'custom', label: '自定义 (cm)', verbose: '全部: 自定义 (cm)' }]);
  // "清除"选项已撤（2026-08-27 用户定案：与"不设置"语义重复）

  function setBatchTriggerLabel(label) {
    batchTrigger.textContent = label;
  }

  function closeBatchDropdown() {
    batchDropdown.classList.remove('open');
  }

  (function initBatchMenu() {
    BATCH_OPTIONS.forEach(function (opt) {
      var optionElement = document.createElement('div');
      optionElement.className = 'menu-option' + (opt.value === '' ? ' selected' : '');
      optionElement.textContent = opt.verbose || opt.label;
      optionElement.dataset.value = opt.value;
      optionElement.addEventListener('click', function () {
        // 选中态迁移
        Array.prototype.forEach.call(batchMenu.children, function (c) { c.classList.remove('selected'); });
        optionElement.classList.add('selected');
        closeBatchDropdown();
        applyBatchSize(opt.value, opt.label);
      });
      batchMenu.appendChild(optionElement);
    });
  })();

  batchTrigger.addEventListener('click', function (event) {
    event.stopPropagation();
    // 三下拉互斥（第二十九次修订两下拉口径扩展）：开尺寸菜单时收起形状/
    // 线宽菜单（同排相邻，双开错位）
    if (shapeDropdown.classList.contains('open')) shapeDropdown.classList.remove('open');
    if (widthDropdown.classList.contains('open')) widthDropdown.classList.remove('open');
    batchDropdown.classList.toggle('open');
  });
  document.addEventListener('click', function (event) {
    if (batchDropdown.classList.contains('open') && !batchDropdown.contains(event.target)) {
      closeBatchDropdown();
    }
  });

  // 提交后复位（批级值 + trigger 文案 + 选中态回"不设置"——下一批从零开始，
  // 2026-08-27 用户定案"批量值只作用于本批"）
  function resetBatchSizeSelection() {
    batchSizeCm = null;
    setBatchTriggerLabel('不设置');
    Array.prototype.forEach.call(batchMenu.children, function (c) {
      c.classList.toggle('selected', c.dataset.value === '');
    });
  }

  function applyBatchSize(value, label) {
    if (value === '') {
      batchSizeCm = null;
      setBatchTriggerLabel('不设置');
      renderUploadList();
      return;
    }
    if (value === 'custom') {
      var input = window.prompt('输入统一打印尺寸（厘米，5-33，上限=打印机最大幅面）', '15');
      var parsed = parseFloat(input);
      if (input != null && !isNaN(parsed) && parsed >= 5 && parsed <= 33) {
        batchSizeCm = parsed;
        setBatchTriggerLabel(parsed + 'cm');
      } else {
        if (input != null) window.alert('请输入 5-33 之间的数字（33cm 为打印机最大幅面）');
        // 取消输入：批级值与选中态一并回"不设置"（不留隐藏残留值）
        batchSizeCm = null;
        Array.prototype.forEach.call(batchMenu.children, function (c) {
          c.classList.toggle('selected', c.dataset.value === '');
        });
        setBatchTriggerLabel('不设置');
        return;
      }
    } else {
      batchSizeCm = parseFloat(value);
      setBatchTriggerLabel(label);
    }
    renderUploadList();
  }

  // ---- 统一线宽批级选择（第三十四次修订：1–2mm 步进 0.1，默认跟随配置） ----
  // 提交时统一写入各图 crop_meta.outline_width_mm；后端合法区间 1.0–2.0，
  // 缺省（默认档）不写键 → 服务端用 PT_OUTLINE_WIDTH_MM 配置值。
  var widthDropdown = document.getElementById('batch-width-dropdown');
  var widthTrigger = document.getElementById('batch-width-trigger');
  var widthMenu = document.getElementById('batch-width-menu');
  var batchOutlineWidthMm = null;
  var WIDTH_OPTIONS = [{ value: '', label: '默认' }];
  for (var wm = 10; wm <= 20; wm++) {
    WIDTH_OPTIONS.push({ value: String(wm / 10), label: (wm / 10) + 'mm' });
  }

  (function initWidthMenu() {
    WIDTH_OPTIONS.forEach(function (opt) {
      var optionElement = document.createElement('div');
      optionElement.className = 'menu-option' + (opt.value === '' ? ' selected' : '');
      optionElement.textContent = opt.label;
      optionElement.dataset.value = opt.value;
      optionElement.addEventListener('click', function () {
        Array.prototype.forEach.call(widthMenu.children, function (c) { c.classList.remove('selected'); });
        optionElement.classList.add('selected');
        widthDropdown.classList.remove('open');
        batchOutlineWidthMm = opt.value === '' ? null : parseFloat(opt.value);
        widthTrigger.textContent = opt.label;
      });
      widthMenu.appendChild(optionElement);
    });
  })();

  widthTrigger.addEventListener('click', function (event) {
    event.stopPropagation();
    // 三下拉互斥：开线宽菜单时收起形状/尺寸菜单
    if (shapeDropdown.classList.contains('open')) shapeDropdown.classList.remove('open');
    closeBatchDropdown();
    widthDropdown.classList.toggle('open');
  });
  document.addEventListener('click', function (event) {
    if (widthDropdown.classList.contains('open') && !widthDropdown.contains(event.target)) {
      widthDropdown.classList.remove('open');
    }
  });

  // 提交后复位（与形状/尺寸同规则：批级值只作用于本批）
  function resetBatchWidthSelection() {
    batchOutlineWidthMm = null;
    widthTrigger.textContent = '默认';
    Array.prototype.forEach.call(widthMenu.children, function (c) {
      c.classList.toggle('selected', c.dataset.value === '');
    });
  }

  // ---- 统一形状批级选择（第二十九次修订；第三十次修订改"后改优先"） ----
  // 未显式裁剪的图提交时以批级形状为默认声明（无框——后端 CropStep 对无框
  // 形状按默认框塑形〔第三十六次修订：与单独裁剪弹层默认框同几何〕、
  // rectangle 整图直通）。后改优先（第三十次修订）：切换统一
  // 形状 = 全局重声明——逐图已设的形状/裁剪框一并重置跟随统一；之后逐图
  // 再裁剪则自声明优先到下一次统一切换（最后动作优先）。
  // 矩形类批级形状=直通零变化（回显不显标签不画预览，直通不撒谎）。
  var shapeDropdown = document.getElementById('batch-shape-dropdown');
  var shapeTrigger = document.getElementById('batch-shape-trigger');
  var shapeMenu = document.getElementById('batch-shape-menu');
  var batchCropShape = 'rectangle'; // 批级默认形状（rectangle=整图直通）
  var SHAPE_DROPDOWN_OPTIONS = [
    { value: 'rectangle', label: '自由矩形', verbose: '自由矩形（默认）' },
    { value: 'circle', label: '圆形' },
    { value: 'square', label: '正方形' },
    { value: 'rectangle-fixed', label: '长方形' },
    { value: 'heart', label: '爱心' },
    { value: 'star', label: '星形' },
  ];

  // 批级形状预览是否画形状合成（rectangle 全图直通画了也无视觉差异，不画；
  // 其余形状有居中内接几何差异，画预览所见即后端塑形结果——第二十九次修订）
  function isShapedBatchDefault() {
    return batchCropShape !== 'rectangle';
  }

  (function initShapeMenu() {
    SHAPE_DROPDOWN_OPTIONS.forEach(function (opt) {
      var optionElement = document.createElement('div');
      optionElement.className = 'menu-option' + (opt.value === 'rectangle' ? ' selected' : '');
      optionElement.textContent = opt.verbose || opt.label;
      optionElement.dataset.value = opt.value;
      optionElement.addEventListener('click', function () {
        Array.prototype.forEach.call(shapeMenu.children, function (c) { c.classList.remove('selected'); });
        optionElement.classList.add('selected');
        shapeDropdown.classList.remove('open');
        batchCropShape = opt.value;
        shapeTrigger.textContent = opt.label;
        // 后改优先（第三十次修订）：统一切换=全局重声明——逐图已设的形状/框
        // 一并重置为整图塑形（cropMeta=null 提交时回落批级默认），toast 明示
        var resetCount = 0;
        pendingImages.forEach(function (item) {
          if (item.cropMeta) { item.cropMeta = null; resetCount++; }
        });
        if (resetCount > 0) showToast('已按「' + opt.label + '」重置 ' + resetCount + ' 张图的形状声明');
        renderUploadList(); // 全部图的形状标签/预览即时刷新
      });
      shapeMenu.appendChild(optionElement);
    });
  })();

  shapeTrigger.addEventListener('click', function (event) {
    event.stopPropagation();
    if (batchDropdown.classList.contains('open')) closeBatchDropdown();
    if (widthDropdown.classList.contains('open')) widthDropdown.classList.remove('open');
    shapeDropdown.classList.toggle('open');
  });
  document.addEventListener('click', function (event) {
    if (shapeDropdown.classList.contains('open') && !shapeDropdown.contains(event.target)) {
      shapeDropdown.classList.remove('open');
    }
  });

  // 提交后复位（批级形状与尺寸同规则：只作用于本批，下一批回默认）
  function resetBatchShapeSelection() {
    batchCropShape = 'rectangle';
    shapeTrigger.textContent = '自由矩形';
    Array.prototype.forEach.call(shapeMenu.children, function (c) {
      c.classList.toggle('selected', c.dataset.value === 'rectangle');
    });
  }

  // 未裁剪图的批级形状预览：与后端 CropStep 同几何——形状默认框（第三十六
  // 次修订：图内居中最大形状包围盒宽高比框，与单独裁剪弹层默认框一致）框
  // 裁后形状满幅 clip，所见即后端塑形结果（统一形状 ≡ 单独裁剪不动框确认）
  function attachShapeOnlyPreview(imgElement, item, itemIndex) {
    loadIntoImageElement(item.originalFile).then(function (imageEl) {
      if (pendingImages[itemIndex] !== item) return; // 列表已变（移除/重排）
      var natural = { w: imageEl.naturalWidth, h: imageEl.naturalHeight };
      if (natural.w < 1 || natural.h < 1) return;
      var supersample = 2; // 小缩略图形状边缘抗锯齿（与声明预览同口径）
      // 默认框（第三十六次修订对齐）：画布=框幅（形状包围盒比例），原图按
      // 框裁居中 draw——形状撑满框，与 attachCroppedPreview 几何一致
      var box = defaultShapeBox(natural.w, natural.h, batchCropShape);
      var previewCanvas = document.createElement('canvas');
      previewCanvas.width = box.w * supersample;
      previewCanvas.height = box.h * supersample;
      var ctx = previewCanvas.getContext('2d');
      buildShapePath(ctx, batchCropShape, previewCanvas.width, previewCanvas.height);
      ctx.clip();
      ctx.drawImage(
        imageEl, box.x, box.y, box.w, box.h,
        0, 0, previewCanvas.width, previewCanvas.height
      );
      imgElement.src = previewCanvas.toDataURL('image/png');
    }).catch(function () { /* 预览失败保持原图，不阻塞 */ });
  }

  // ---- 裁剪交互（cropperjs 1.x API；形状=宽高比约束 + crop 后遮罩塑形） ----

  var SHAPE_ASPECT_RATIOS = {
    'rectangle': NaN,       // 自由
    'circle': 1,
    'square': 1,
    'rectangle-fixed': 1.5,
    // 心/星按真实包围盒宽高比锁定（2026-08-26 15:39：旧锁 1:1 正方形框在
    // 横图上无法拉满宽——心形归一化包围盒 32:28.9≈1.107、星形 1.9:1.81≈1.05；
    // 选框=形状包围盒，拉到极限形状正好贴满，无剩余不可用空间）
    'heart': 32 / 28.9,
    'star': 1.902 / 1.809,
  };

  // 形状默认框（第三十六次修订）：图内居中最大形状包围盒宽高比框——与
  // 后端 outline.default_shape_box 同源同几何（无框统一形状 ≡ 单独裁剪
  // 弹层 autoCropArea=1 + 宽高比锁定的默认框）；改宽高比表必须两端同改
  function defaultShapeBox(width, height, shapeValue) {
    var aspect = SHAPE_ASPECT_RATIOS[shapeValue];
    if (aspect === undefined || isNaN(aspect) || width < 1 || height < 1) {
      return { x: 0, y: 0, w: width, h: height }; // rectangle/free：整图即框
    }
    var boxW, boxH;
    if (width / height >= aspect) {
      boxH = height;
      boxW = Math.round(height * aspect);
    } else {
      boxW = width;
      boxH = Math.round(width / aspect);
    }
    boxW = Math.min(boxW, width);
    boxH = Math.min(boxH, height);
    return { x: (width - boxW) / 2, y: (height - boxH) / 2, w: boxW, h: boxH };
  }

  function openCropModal(itemIndex) {
    cropTargetIndex = itemIndex;
    // 预选当前生效形状（第三十次修订）：该图自声明优先，否则统一形状——
    // 取代 2026-08-26"每次打开重置自由矩形"（旧口径防的是误用上一张的
    // 弹层选择；预选当前生效值不属此列，且后改优先下统一形状即当前基准）
    var item = pendingImages[itemIndex];
    var effectiveShape = (item.cropMeta && item.cropMeta.shape) || batchCropShape;
    cropShapeSelect.value = (SHAPE_ASPECT_RATIOS[effectiveShape] !== undefined) ? effectiveShape : 'rectangle';
    // 裁剪源固定用原图（已裁剪项重新裁剪时不受上次裁剪结果影响）
    var sourceUrl = pendingImages[itemIndex].originalPreviewUrl || pendingImages[itemIndex].previewUrl;
    cropModal.classList.add('active');
    // 先挂监听再赋 src，且覆盖"图片已在缓存解码完成"的情况
    // （注意：不能用 cropperjs 的 'ready' 事件做触发器——那是 new Cropper() 建成后才派发的事件，先有鸡才有蛋）
    if (cropSourceImage.src === sourceUrl && cropSourceImage.complete) {
      startCropper();
    } else {
      cropSourceImage.addEventListener('load', startCropper, { once: true });
      cropSourceImage.src = sourceUrl;
    }
  }

  function startCropper() {
    destroyCropper();
    var shapeValue = cropShapeSelect.value;
    var aspectRatio = SHAPE_ASPECT_RATIOS[shapeValue];
    cropperInstance = new Cropper(cropSourceImage, {
      viewMode: 1,
      aspectRatio: isNaN(aspectRatio) ? NaN : aspectRatio,
      autoCropArea: 1, // 选框默认覆盖整图（2026-08-26 需求：客户通常要全图入形状，缩小是主动行为）
      background: false,
      guides: false,   // 形状模式不需要矩形虚线参考线（矩形模式下也由遮罩代替）
      center: false,
      ready: function () {
        attachShapeOverlay(shapeValue);
        updateCropSizeLabel();
      },
      // 事件回调走 options（1.x 无实例 on/bind；此前 bind('cropmove') 是无效调用）
      cropmove: updateCropSizeLabel,
      crop: function () { updateCropSizeLabel(); redrawShapeOverlay(); },
    });
    cropperInstance.crop();
  }

  function updateCropSizeLabel() {
    if (!cropperInstance) return;
    var cropBoxData = cropperInstance.getCropBoxData();
    if (cropBoxData && cropBoxData.width) {
      cropSizeLabel.textContent = Math.round(cropBoxData.width) + ' × ' + Math.round(cropBoxData.height);
    }
  }

  // ---- 形状遮罩预览（cropperjs 1.x 裁剪框恒为矩形；成品塑形在后端 CropStep，
  //      这里在交互层同步形状观感：view-box 上叠 canvas，形状外压暗 + 形状描边） ----

  function attachShapeOverlay(shapeValue) {
    detachShapeOverlay();
    if (shapeValue === 'rectangle' || shapeValue === 'rectangle-fixed' || shapeValue === 'square') return;
    var viewBox = cropSourceImage.parentElement.querySelector('.cropper-view-box');
    var cropBox = viewBox && viewBox.parentElement; // .cropper-crop-box
    if (!cropBox) return;
    var overlayCanvas = document.createElement('canvas');
    overlayCanvas.className = 'shape-overlay-canvas';
    cropBox.insertBefore(overlayCanvas, cropBox.firstChild);
    // 形状模式：摘掉矩形框全部可视附件，形状遮罩即唯一裁剪框（CSS 见 index.html）
    cropBox.classList.add('shape-mode');
    cropperShapeOverlay = overlayCanvas;
    redrawShapeOverlay();
  }

  function detachShapeOverlay() {
    if (cropperShapeOverlay) {
      var cropBox = cropperShapeOverlay.parentNode;
      if (cropBox) cropBox.classList.remove('shape-mode');
      if (cropperShapeOverlay.parentNode) cropperShapeOverlay.parentNode.removeChild(cropperShapeOverlay);
      cropperShapeOverlay = null;
    }
  }

  function redrawShapeOverlay() {
    if (!cropperInstance || !cropperShapeOverlay) return;
    var cropBoxData = cropperInstance.getCropBoxData();
    if (!cropBoxData || !cropBoxData.width) return;
    var overlayWidth = Math.round(cropBoxData.width);
    var overlayHeight = Math.round(cropBoxData.height);
    if (overlayWidth <= 0 || overlayHeight <= 0) return;
    if (cropperShapeOverlay.width !== overlayWidth || cropperShapeOverlay.height !== overlayHeight) {
      cropperShapeOverlay.width = overlayWidth;
      cropperShapeOverlay.height = overlayHeight;
    }
    var context = cropperShapeOverlay.getContext('2d');
    context.clearRect(0, 0, overlayWidth, overlayHeight);
    var shapeValue = cropShapeSelect.value;
    // 形状外半透明压暗（对应 modal 暗背景语义）
    context.fillStyle = 'rgba(0, 0, 0, 0.45)';
    context.fillRect(0, 0, overlayWidth, overlayHeight);
    // 抠出形状内部（destination-out 擦除形状区域）
    context.save();
    context.globalCompositeOperation = 'destination-out';
    buildShapePath(context, shapeValue, overlayWidth, overlayHeight);
    context.fill();
    context.restore();
    // 形状描边（引导线，与 view-box 蓝框同色系）
    context.strokeStyle = 'rgba(51, 153, 255, 0.9)';
    context.lineWidth = 2;
    buildShapePath(context, shapeValue, overlayWidth, overlayHeight);
    context.stroke();
  }

  function destroyCropper() {
    if (cropperInstance) {
      cropperInstance.destroy();
      cropperInstance = null;
    }
    detachShapeOverlay(); // destroy 会重建/移除 DOM，遮罩一并清理防悬挂引用
  }

  cropShapeSelect.addEventListener('change', startCropper);
  cropCancelButton.addEventListener('click', closeCropModal);

  // 桌面肌肉记忆（2026-08-27）：ESC 关弹层；点遮罩空白处关闭（点在图/头部/
  // 底部控件上不关——裁剪操作区误触代价高）
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && cropModal.classList.contains('active')) closeCropModal();
  });
  cropModal.addEventListener('click', function (event) {
    if (event.target === cropModal || event.target.id === 'crop-stage') closeCropModal();
  });

  // Ctrl+V 粘贴上传（2026-08-27 桌面端：聊天窗口收图→直接粘贴，省"先存盘"）
  document.addEventListener('paste', function (event) {
    if (cropModal.classList.contains('active')) return; // 裁剪中不抢粘贴
    var pastedFiles = event.clipboardData && event.clipboardData.files;
    if (!pastedFiles || !pastedFiles.length) return;
    var imageFiles = Array.prototype.filter.call(pastedFiles, function (f) {
      return f.type.indexOf('image/') === 0;
    });
    if (!imageFiles.length) return;
    event.preventDefault();
    acceptFiles(imageFiles);
  });

  function closeCropModal() {
    destroyCropper();
    cropModal.classList.remove('active');
    cropTargetIndex = -1;
  }

  cropApplyButton.addEventListener('click', function () {
    if (!cropperInstance || cropTargetIndex < 0) { closeCropModal(); return; }
    // 声明式裁剪（2026-08-26 定案）：前端不做像素裁剪/形状遮罩导出，只记录
    // 形状 + 框参数（data 相对原图像素坐标；frame=原图尺寸，后端据此把框
    // 等比映射到管线实际图幅——生成式交付幅可能与原图不同）——真正的裁剪
    // 由后端 CropStep 运行时执行；主图恒传原图，同图切换形状零重复模型外呼
    var shapeValue = cropShapeSelect.value;
    var targetItem = pendingImages[cropTargetIndex];
    var cropData = cropperInstance.getData();
    var containerData = cropperInstance.getContainerData ? cropperInstance.getContainerData() : {};
    var imageData = cropperInstance.getImageData ? cropperInstance.getImageData() : {};
    var frameWidth = Math.round((imageData.naturalWidth || containerData.width || cropData.width) || 0);
    var frameHeight = Math.round((imageData.naturalHeight || containerData.height || cropData.height) || 0);
    targetItem.cropMeta = {
      shape: shapeValue,
      frame: { width: frameWidth, height: frameHeight },
      box: { width: Math.round(cropData.width || 0), height: Math.round(cropData.height || 0) },
      data: { x: Math.round(cropData.x || 0), y: Math.round(cropData.y || 0), width: Math.round(cropData.width || 0), height: Math.round(cropData.height || 0) },
    };
    closeCropModal();
    renderUploadList();
  });


  // 声明式裁剪的缩略图预览：按 cropMeta（框+形状）在原图上现画形状效果。
  // 异步生成 dataURL 替换 img.src；条目被移除/重裁后回调作废（索引+指纹双查）。
  function attachCroppedPreview(imgElement, item, itemIndex) {
    loadIntoImageElement(item.originalFile).then(function (imageEl) {
      if (pendingImages[itemIndex] !== item) return; // 列表已变（移除/重排）
      var meta = item.cropMeta;
      var box = meta.data;
      var natural = { w: imageEl.naturalWidth, h: imageEl.naturalHeight };
      var cropRect = {
        x: Math.max(0, Math.min(box.x, natural.w - 1)),
        y: Math.max(0, Math.min(box.y, natural.h - 1)),
        w: Math.min(box.width, natural.w - Math.max(0, box.x)),
        h: Math.min(box.height, natural.h - Math.max(0, box.y)),
      };
      if (cropRect.w < 1 || cropRect.h < 1) return; // 框无效：保持原图回显
      // 预览超采样 2x：小缩略图上形状边缘只有 1-2px，clip 抗锯齿采样不足
      // 显得锯齿重（2026-08-26 用户实测反馈）——canvas 画 2 倍尺寸再由
      // <img> CSS 缩到卡片宽，等效 2x 超采样，边缘过渡带翻倍平滑
      var supersample = 2;
      var previewCanvas = document.createElement('canvas');
      previewCanvas.width = cropRect.w * supersample;
      previewCanvas.height = cropRect.h * supersample;
      var ctx = previewCanvas.getContext('2d');
      if (meta.shape !== 'rectangle') {
        // 形状遮罩：clip 后画图，形状外保持透明（棋盘格底透出形状观感）。
        // square/rectangle-fixed 也走 clip（第二十九次修订：居中内接几何，
        // buildShapePath 同源；带框裁剪预览下框幅即画布，正方形/3:2 内接可视）
        buildShapePath(ctx, meta.shape, previewCanvas.width, previewCanvas.height);
        ctx.clip();
      }
      ctx.drawImage(
        imageEl, cropRect.x, cropRect.y, cropRect.w, cropRect.h,
        0, 0, previewCanvas.width, previewCanvas.height
      );
      imgElement.src = previewCanvas.toDataURL('image/png');
    }).catch(function () { /* 预览失败保持原图，不阻塞 */ });
  }

  function buildShapePath(context, shapeValue, width, height) {
    var centerX = width / 2, centerY = height / 2, radius = Math.min(width, height) / 2;
    if (shapeValue === 'square') {
      // 居中正方形（第二十九次修订：与后端 crop_shape_region_mask 同源——
      // square/rectangle-fixed 不再全图直通，选了形状就要看得见形状）
      var side = Math.min(width, height);
      context.beginPath();
      context.rect((width - side) / 2, (height - side) / 2, side, side);
      context.closePath();
      return;
    }
    if (shapeValue === 'rectangle-fixed') {
      // 居中 3:2 长方形（宽高比与弹层 SHAPE_ASPECT_RATIOS 同源）
      var rectW, rectH;
      if (Math.round(height * 1.5) <= width) {
        rectH = height;
        rectW = Math.round(height * 1.5);
      } else {
        rectW = width;
        rectH = Math.round(width / 1.5);
      }
      context.beginPath();
      context.rect((width - rectW) / 2, (height - rectH) / 2, rectW, rectH);
      context.closePath();
      return;
    }
    if (shapeValue === 'circle') {
      context.beginPath();
      context.arc(centerX, centerY, radius, 0, Math.PI * 2);
      context.closePath();
      return;
    }
    if (shapeValue === 'heart') {
      // 参数化心形曲线（Classic heart curve）——包围盒最大化（2026-08-26 15:39：
      // 归一化包围盒 32×28.9，与后端 crop_shape_region_mask 同源；旧"内接圆÷17"
      // 对角留白大）。scale = min(W/32, H/28.9)，y 垂直居中（公式中心 ≈2.55）
      var heartScale = Math.min(width / 32, height / 28.9);
      context.beginPath();
      for (var angleStep = 0; angleStep <= 200; angleStep++) {
        var theta = (angleStep / 200) * Math.PI * 2;
        var heartX = 16 * Math.pow(Math.sin(theta), 3);
        var heartY = -(13 * Math.cos(theta) - 5 * Math.cos(2 * theta) - 2 * Math.cos(3 * theta) - Math.cos(4 * theta));
        var plotX = centerX + heartX * heartScale;
        var plotY = centerY + (heartY - 2.55) * heartScale;
        if (angleStep === 0) context.moveTo(plotX, plotY); else context.lineTo(plotX, plotY);
      }
      context.closePath();
      return;
    }
    if (shapeValue === 'star') {
      // 尖锐十顶点星（内半径 0.44r）——精确包围盒最大化：宽 1.902r、高 1.809r、
      // y∈[-1,0.809] 顶角非对称 → r=min(W/1.902,H/1.809)，y 偏移 +0.0955r
      // 垂直居中（2026-08-26 15:53：顶角截断修复，与后端同源）
      var starRadius = Math.min(width / 1.902, height / 1.809);
      var starCenterY = centerY + 0.0955 * starRadius;
      context.beginPath();
      for (var vertexIndex = 0; vertexIndex < 10; vertexIndex++) {
        var vertexAngle = -Math.PI / 2 + vertexIndex * Math.PI / 5;
        var vertexRadius = vertexIndex % 2 === 0 ? starRadius : starRadius * 0.44; // 尖锐标准星（2026-08-26 15:53 撤回胖星）
        var vertexX = centerX + vertexRadius * Math.cos(vertexAngle);
        var vertexY = starCenterY + vertexRadius * Math.sin(vertexAngle);
        if (vertexIndex === 0) context.moveTo(vertexX, vertexY); else context.lineTo(vertexX, vertexY);
      }
      context.closePath();
      return;
    }
    // 兜底：全图矩形
    context.beginPath();
    context.rect(0, 0, width, height);
    context.closePath();
  }

  // ---- 提交与轮询 ----

  submitButton.addEventListener('click', submitJob);

  async function submitJob() {
    if (pendingImages.length === 0) return;
    submitButton.disabled = true;
    submitButton.textContent = '提交中…';
    var formData = new FormData();
    var cropMetaBySeq = {};
    pendingImages.forEach(function (item, itemIndex) {
      // 声明式裁剪（2026-08-26）：主图恒传原图（未再本地裁剪），crop_meta
      // 声明形状+框；后端管线在原始域处理 + CropStep 运行时裁剪——
      // 同图不同形状共享全部服务端缓存，模型零重复外呼
      formData.append('images', item.originalFile, 'image_' + (itemIndex + 1) + '.png');
      // 每图必有形状（2026-08-25 需求；第二十九次修订）：显式裁剪的图用自
      // 声明（shape+框）优先；未显式裁剪的图以批级"统一形状"为默认声明
      // （无框——后端 CropStep 按形状默认框塑形〔第三十六次修订：与单独
      // 裁剪不动默认框直接确认同几何〕、rectangle 直通；
      // 默认 rectangle=批级初始值，语义与 2026-08-27 默认值修订一致）
      // 尺寸声明（2026-08-28 第二十八次修订）：批级"统一尺寸"值提交时统一
      // 写入各图 crop_meta.size.cm（逐图尺寸入口已撤）
      var seqMeta = item.cropMeta || { shape: batchCropShape, default: true, box: null };
      if (batchSizeCm) seqMeta.size = { cm: batchSizeCm };
      if (batchOutlineWidthMm) seqMeta.outline_width_mm = batchOutlineWidthMm; // 第三十四次修订
      cropMetaBySeq[String(itemIndex + 1)] = seqMeta;
    });
    formData.append('crop_meta', JSON.stringify(cropMetaBySeq));
    try {
      var response = await fetch('/api/jobs', { method: 'POST', body: formData });
      if (!response.ok) {
        var errorBody = await response.json().catch(function () { return {}; });
        throw new Error((errorBody.detail) || ('提交失败（' + response.status + '）'));
      }
      var jobResponse = await response.json();
      submittingJobId = jobResponse.job_id;
      pendingImages.forEach(function (item) {
        URL.revokeObjectURL(item.previewUrl);
        if (item.originalPreviewUrl && item.originalPreviewUrl !== item.previewUrl) {
          URL.revokeObjectURL(item.originalPreviewUrl);
        }
      });
      pendingImages = [];
      renderUploadList();
      // 提交后统一尺寸/形状回默认（2026-08-27 用户定案：批量值只作用于本批，
      // 下一批从零开始——trigger 文案与选中态一并复位）
      resetBatchSizeSelection();
      resetBatchShapeSelection();
      resetBatchWidthSelection();
      // 双栏同屏（2026-08-27 第六次修订）：右栏常驻无需显隐切换与滚动跟随
      // 居中口径同 showResultPlaceholder（2026-08-29：提示恒在卡片正中）
      resultList.style.display = 'grid';
      resultList.style.placeItems = 'center';
      resultList.innerHTML = '<div class="status-text processing" style="padding:12px;">处理中，请稍候…</div>';
      serverClockOffsetMs = 0; // 钟差待首轮轮询 server_time 校正
      startPolling();
    } catch (submitError) {
      alert(submitError.message || '提交失败，请重试');
      submitButton.disabled = false;
      submitButton.textContent = '开始处理';
    }
  }

  function startPolling() {
    stopPolling();
    pollTimerId = setInterval(pollJobStatus, POLL_INTERVAL_MS);
    if (cancelJobButton) cancelJobButton.disabled = false; // 处理中可取消（第三十七次修订）
    pollJobStatus(); // 立即首查
  }

  function stopPolling() {
    if (pollTimerId) { clearInterval(pollTimerId); pollTimerId = null; }
    if (cancelJobButton) cancelJobButton.disabled = true; // 轮询停=无在途任务可取消
  }

  async function pollJobStatus() {
    if (!submittingJobId) return;
    try {
      var response = await fetch('/api/jobs/' + submittingJobId);
      if (!submittingJobId) return; // 已取消/已重置：丢弃在途迟到响应（第三十七次修订——防重绘覆盖引导态）
      if (response.status === 404) {
        stopPolling();
        stopElapsedTicker(); // 计时随批次终止（2026-08-28）
        resultList.innerHTML = '<div class="status-text failed">批次已过期，请重新上传</div>';
        return;
      }
      if (!response.ok) return; // 瞬时错误：下轮重试
      var jobStatus = await response.json();
      syncServerClock(jobStatus.server_time); // 钟差校正先于渲染（计时锚点用）
      renderResults(jobStatus);
      if (jobStatus.status === 'completed') {
        stopPolling();
        // 全部终态：终态卡是静态文本无需 ticker；有动态 span 残留（防御）再停
        if (!resultList.querySelector('.elapsed-timer[data-start-ms]')) stopElapsedTicker();
      }
    } catch (networkError) { /* 网络抖动下轮重试 */ }
  }

  // ---- 顶部小弹窗 toast（2026-08-27：按钮反馈统一入口） ----
  var toastLayer = document.getElementById('toast-layer');
  function showToast(message, isError) {
    if (!toastLayer) return;
    var toastItem = document.createElement('div');
    toastItem.className = 'toast-item' + (isError ? ' toast-error' : '');
    toastItem.textContent = message;
    toastLayer.appendChild(toastItem);
    requestAnimationFrame(function () { toastItem.classList.add('show'); });
    setTimeout(function () {
      toastItem.classList.remove('show');
      setTimeout(function () { if (toastItem.parentNode) toastItem.parentNode.removeChild(toastItem); }, 300);
    }, 2200);
  }

  // ---- 结果渲染与分端交付 ----

  // 全部执行步骤展示（2026-08-26 补裁剪/缩放）；crop 记档是声明 JSON 特判
  // 管线执行顺序（第二十七次修订）：去水印→填充→缩放→裁剪→描边（2026-08-26 21:54
  // 补 resize 格；值=实际分辨率，异步从结果图回填，回填前显示状态词）
  var STAGE_LABELS = {watermark: '去水印', fill: '填充', resize: '分辨率', crop: '裁剪', outline: '描边'};
  var STAGE_VALUE_LABELS = { 'done': '已处理', 'skipped': '跳过', 'fallback': '保留原样', 'done(api)': 'AI处理', 'done(degraded)': 'AI降级', '白色背景': '白底替换', 'done(upscaled)': '已自动提升' };
  var SHAPE_CN = { circle: '圆形', square: '正方形', rectangle: '自由矩形', 'rectangle-fixed': '长方形', heart: '爱心', star: '星形', free: '自由矩形' };
  // quality_hint 提示已全线撤销（2026-08-27 用户定案：heavy-watermark 提示
  // 也不再显示——结果卡只保留步骤标签，无警示标签）；HINT_LABELS 保留空表
  // 供字段兼容，渲染处空值直接跳过
  var HINT_LABELS = {};

  var batchDownloadButton = document.getElementById('batch-download-button');
  var batchDeleteButton = document.getElementById('batch-delete-button');
  var cancelJobButton = document.getElementById('cancel-job-button');
  var batchStatus = document.getElementById('batch-status');
  var batchResultUrls = []; // 当前批次的 completed 结果 URL 序列（批量动作用）

  // 会话级删除（2026-08-27 终版：去掉逐卡勾选框，标题行"全部删除"+确认弹窗）：
  // removedImageIds 过滤渲染与批量动作——只整理视图不调后端（结果文件由 24h
  // TTL 统一清理）；删除前 confirm 友好提示，确认才删
  var latestJobStatus = null;
  var removedImageIds = {};

  function renderResults(jobStatus) {
    latestJobStatus = jobStatus;
    // 还原占位态的居中布局（showResultPlaceholder 设的 grid/center——
    // 结果卡要用回 .image-list 网格布局）
    resultList.style.display = '';
    resultList.style.placeItems = '';
    resultList.innerHTML = '';
    batchResultUrls = [];
    jobStatus.images.forEach(function (imageStatus) {
      if (removedImageIds[imageStatus.image_id]) return; // 已删不渲染
      resultList.appendChild(buildResultCard(imageStatus));
      if (imageStatus.status === 'completed' && imageStatus.result_url) {
        batchResultUrls.push({ seq: imageStatus.seq, url: imageStatus.result_url });
      }
    });
    updateBatchActions();
  }

  function removeImages(imageIds) {
    imageIds.forEach(function (id) { removedImageIds[id] = true; });
    if (latestJobStatus) renderResults(latestJobStatus);
    if (!resultList.children.length) showResultPlaceholder(); // 全删完回引导态
  }

  function updateDeleteButtonState() {
    if (!batchDeleteButton) return;
    // "全部删除"（2026-08-27 终版）：有结果即可用，无结果禁用
    batchDeleteButton.disabled = batchResultUrls.length === 0;
    batchDeleteButton.textContent = '全部删除';
  }

  if (batchDeleteButton) {
    batchDeleteButton.addEventListener('click', function () {
      if (!batchResultUrls.length) return;
      // 友好确认弹窗（2026-08-27 用户定案文案）：确认才删，取消不动
      var confirmed = window.confirm('删除之后不可再次取回，请确保已经保存后再删除');
      if (!confirmed) return;
      var toRemove = Object.keys(removedImageIds).length; // 仅用于计数反馈
      var allIds = [];
      latestJobStatus.images.forEach(function (imageStatus) {
        if (!removedImageIds[imageStatus.image_id]) allIds.push(imageStatus.image_id);
      });
      removeImages(allIds);
      showToast('已删除 ' + allIds.length + ' 张');
    });
  }

  function updateBatchActions() {
    // 标题行"下载/全部删除"（2026-08-27 终版）：有结果即可用
    if (batchDownloadButton) {
      batchDownloadButton.disabled = batchResultUrls.length === 0;
    }
    var batchContainer = document.getElementById('batch-actions');
    batchContainer.style.display = 'none';
    if (batchStatus) {
      batchStatus.textContent = '';
    }
    updateDeleteButtonState();
  }

  // 批量下载：ZIP 打包单文件落盘（2026-08-26 16:55 改版——旧逐张 a[download]
  // 被浏览器多文件拦截 + 逐张询问；ZIP 一次点击一个文件，解压即得全部独立 PNG）
  if (batchDownloadButton) {
    batchDownloadButton.addEventListener('click', async function () {
      if (!batchResultUrls.length) return;
      batchDownloadButton.disabled = true;
      if (batchStatus) batchStatus.textContent = '打包中（' + batchResultUrls.length + ' 张）…';
      try {
        var builder = StoreZip.builder();
        for (var i = 0; i < batchResultUrls.length; i++) {
          var response = await fetch(batchResultUrls[i].url);
          var bytes = new Uint8Array(await response.arrayBuffer());
          builder.add('pattern_' + batchResultUrls[i].seq + '.png', bytes);
        }
        var zipBlob = builder.build();
        var zipUrl = URL.createObjectURL(zipBlob);
        var link = document.createElement('a');
        link.href = zipUrl;
        link.download = 'patterns_' + batchResultUrls.length + '张.zip';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(zipUrl); }, 10000);
        if (batchStatus) batchStatus.textContent = '已打包下载 ' + batchResultUrls.length + ' 张（解压即得独立 PNG）';
      } catch (zipError) {
        if (batchStatus) batchStatus.textContent = '打包失败，请逐张下载';
      }
      batchDownloadButton.disabled = false;
    });
  }

  function buildResultCard(imageStatus) {
    var card = document.createElement('div');
    card.className = 'image-card';
    var resizeTagToFill = null; // 分辨率格：加载完回填实际像素

    // 勾选框已撤（2026-08-27 终版：批量删除改为标题行"全部删除"+确认弹窗）

    var thumbWrap = document.createElement('div');
    thumbWrap.className = 'thumb-wrap';
    var seqBadge = document.createElement('span');
    seqBadge.className = 'seq-badge';
    seqBadge.textContent = String(imageStatus.seq);
    thumbWrap.appendChild(seqBadge);

    var info = document.createElement('div');
    info.className = 'card-info';
    var statusLine = document.createElement('div');
    statusLine.className = 'status-text ' + imageStatus.status;
    statusLine.textContent = {
      queued: '排队中…',
      processing: '处理中…',
      completed: '完成',
      failed: '失败',
    }[imageStatus.status] || imageStatus.status;
    // 消耗计时（2026-08-28 第二十四次修订）：排队中**不计时**（用户定案
    // "只有执行中的图才计时"）；执行中挂动态 span（全局 ticker 500ms 刷新）；
    // 终态定格服务端真值 finished_at - started_at（不再随轮询回涨）
    if (imageStatus.status === 'processing') {
      statusLine.appendChild(createProcessingTimer(imageStatus.started_at));
      ensureElapsedTicker();
    } else if (imageStatus.status === 'completed' || imageStatus.status === 'failed') {
      statusLine.appendChild(createFinalElapsed(imageStatus.started_at, imageStatus.finished_at));
    }
    // queued：无计时 span——排队不计入执行耗时
    info.appendChild(statusLine);

    if (imageStatus.status === 'failed' && imageStatus.error_msg) {
      var errorLine = document.createElement('div');
      errorLine.className = 'status-text failed';
      errorLine.textContent = imageStatus.error_msg;
      info.appendChild(errorLine);
    }

    // 阶段标签（三步独立留痕）
    if (imageStatus.status === 'completed') {
      var stageTags = document.createElement('div');
      stageTags.className = 'stage-tags';
      Object.keys(STAGE_LABELS).forEach(function (stageKey) {
        var stageValue = imageStatus.stage_results[stageKey];
        if (stageKey === 'resize' && !stageValue) stageValue = 'skipped'; // 五步全显：无记录=跳过
        if (!stageValue) return;
        var tag = document.createElement('span');
        if (stageKey === 'crop') {
          // crop 记档=声明 JSON（含 shape；crop_applied 才是执行态）——展示形状名，
          // 无声明记 "skipped"；执行与否用 crop_applied 变清晰（不额外占格）
          var applied = imageStatus.stage_results['crop_applied'];
          var shapeName = '';
          try { shapeName = (JSON.parse(stageValue).shape) || ''; } catch (parseError) { /* 非 JSON 按 skipped */ }
          if (stageValue === 'skipped' || !shapeName) {
            tag.className = 'tag skipped';
            tag.textContent = '裁剪:跳过';
          } else {
            tag.className = 'tag ' + (applied ? 'done' : 'skipped');
            tag.textContent = '裁剪:' + (SHAPE_CN[shapeName] || shapeName) + (applied ? '' : '(默认)');
          }
        } else {
          // 执行失败红显（2026-08-27 用户定案）："该跑但没跑成"与"不用跑"分开——
          // failed（去水印检出但修复失败 / 放大超分失败——2026-08-28 起不交付插值废图）
          // （超分失败走插值）一律红底"执行失败"；skipped=真没执行（无水印/
          // 非棋盘格）保持灰显"跳过"
          var isFailed = stageValue === 'failed' || stageValue === 'done(interpolated)';
          var normalizedValue = stageValue === 'done' ? 'done' : (stageValue === 'skipped' ? 'skipped' : (stageValue === 'fallback' ? 'fallback' : 'done'));
          if (isFailed) normalizedValue = 'failed';
          tag.className = 'tag ' + normalizedValue;
          tag.textContent = STAGE_LABELS[stageKey] + ':' + (isFailed ? '执行失败' : (STAGE_VALUE_LABELS[stageValue] || stageValue));
          if (stageKey === 'resize' && stageValue !== 'skipped') {
            // 值=实际分辨率：等缩略图加载完读 naturalWidth×Height 回填（图是同一张）
            if (isFailed) {
              // 执行失败：保留失败文案，不回填分辨率数字（分辨率虽真但语义误导）
              resizeTagToFill = null;
            } else {
              resizeTagToFill = tag;
            }
          }
        }
        stageTags.appendChild(tag);
      });
      info.appendChild(stageTags);

      // 尾程提示（2026-08-28 用户定案）：源图偏小（放大后清晰度打了折扣）→
      // 黄色建议标签"建议提供更大图片"——不阻塞交付，客户可选换图重提。
      if (imageStatus.quality_hint === 'suggest-larger-source') {
        var suggestTag = document.createElement('div');
        suggestTag.className = 'tag fallback';
        suggestTag.textContent = '建议提供更大尺寸的原图（当前图片放大后清晰度有限）';
        info.appendChild(suggestTag);
      }

      var resultImage = document.createElement('img');
      resultImage.className = 'thumb';
      // 分辨率格回填：先挂 load 再赋 src（缓存图同步完成解码，后挂会错过事件）
      resultImage.addEventListener('load', function () {
        if (resizeTagToFill && resultImage.naturalWidth) {
          resizeTagToFill.textContent = '分辨率:' + resultImage.naturalWidth + '×' + resultImage.naturalHeight;
        }
      });
      resultImage.src = imageStatus.result_url;
      thumbWrap.appendChild(resultImage);
      attachResultZoom(thumbWrap, imageStatus.result_url);

      // 卡片底部动作条（2026-08-27 终版：每张卡保留下载/复制/删除三个独立
      // 按钮；标题行页面级"下载/删除"是批量语义，两级并存各司其职）
      var actions = document.createElement('div');
      actions.className = 'result-actions';
      actions.style.padding = '8px';
      var downloadLink = document.createElement('a');
      downloadLink.className = 'download-link';
      downloadLink.href = imageStatus.result_url;
      downloadLink.download = 'pattern_' + imageStatus.seq + '.png';
      downloadLink.textContent = '下载';
      downloadLink.addEventListener('click', function () {
        showToast('已开始下载 pattern_' + imageStatus.seq + '.png');
      });
      actions.appendChild(downloadLink);
      if (navigator.clipboard && window.ClipboardItem) {
        var copyButton = document.createElement('button');
        copyButton.textContent = '复制';
        copyButton.addEventListener('click', async function () {
          try {
            var clipboardResponse = await fetch(imageStatus.result_url);
            var clipboardBlob = await clipboardResponse.blob();
            await navigator.clipboard.write([new ClipboardItem({ 'image/png': clipboardBlob })]);
            showToast('已复制到剪贴板');
          } catch (clipboardError) {
            showToast('复制失败', true);
          }
        });
        actions.appendChild(copyButton);
      }
      var singleDeleteButton = document.createElement('button');
      singleDeleteButton.className = 'danger';
      singleDeleteButton.textContent = '删除';
      singleDeleteButton.addEventListener('click', function () {
        removeImages([imageStatus.image_id]);
        showToast('已删除 1 张（第 ' + imageStatus.seq + ' 张）');
      });
      actions.appendChild(singleDeleteButton);
      if (isMobileEnvironment()) {
        var longPressHint = document.createElement('div');
        longPressHint.className = 'mobile-hint';
        longPressHint.textContent = '长按上方图片保存到相册';
        actions.appendChild(longPressHint);
      }
      card.appendChild(actions);
      card.appendChild(info);
    } else {
      card.appendChild(info);
    }

    card.insertBefore(thumbWrap, card.firstChild);
    return card;
  }

  // 放弃当前任务回引导态（第三十七次修订：取消任务与再处理一批共用——
  // 停轮询/停计时/清提交号/右栏回引导态/收放大浮层）
  function abandonCurrentJob() {
    stopPolling();
    stopElapsedTicker(); // 计时随会话重置（2026-08-28）
    submittingJobId = null;
    showResultPlaceholder(); // 双栏常驻：回引导态而非隐藏
    hideResultZoom();
  }

  restartButton.addEventListener('click', abandonCurrentJob);

  // 取消任务（第三十七次修订）：处理中放弃等待——会话级口径（同"全部删除"
  // 不调后端），后台批次继续跑完由 24h TTL 清理；confirm 防误点（本批结果
  // 视图取消后不可恢复）
  if (cancelJobButton) {
    cancelJobButton.addEventListener('click', function () {
      if (!submittingJobId || !pollTimerId) return; // 仅处理中可用（可用态随轮询起停）
      if (!window.confirm('取消后本批结果将不再显示，确定取消？')) return;
      abandonCurrentJob();
      showToast('已取消，可重新上传处理');
    });
  }

  // ---- 结果图悬停放大（桌面 hover 查看细节；触屏无 hover 不触发，pointer-events:none 不打断点击/长按） ----

  var resultZoomLayer = document.getElementById('result-zoom-layer');
  var resultZoomImage = resultZoomLayer ? resultZoomLayer.querySelector('img') : null;
  var ZOOM_VIEW_MAX = 800; // 浮层最大边（px）——2026-08-27 桌面端 480→800：缩略图可辨修图质量
  var ZOOM_VIEW_MARGIN = 16; // 距视口边距

  // 悬停放大（2026-08-27 定案：立即放大——勾选框已移出缩略图到信息栏，
  // 冲突源消失，延迟门槛（曾 150ms→1.5s→800ms）全线撤销，mouseenter 即出图）
  function attachResultZoom(thumbWrap, imageUrl) {
    if (!resultZoomLayer || !resultZoomImage) return;
    thumbWrap.classList.add('zoomable');
    // 悬停放大角标（2026-08-27：hover 浮层此前无入口提示，不可发现）
    var zoomHint = document.createElement('span');
    zoomHint.className = 'zoom-hint';
    zoomHint.textContent = '悬停放大';
    thumbWrap.appendChild(zoomHint);
    // 200ms 延迟（2026-08-27 定案：无延迟版鼠标扫过即闪图太吵——短门槛
    // 滤掉扫过/点按钮的路径，真停留立即跟上；离开即取消计时）
    var hoverTimer = null;
    var closeTimer = null;
    var isPointerOverZoomLayer = false;
    thumbWrap.addEventListener('mouseenter', function () {
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () {
        if (resultZoomImage.getAttribute('src') !== imageUrl) resultZoomImage.src = imageUrl;
        resultZoomLayer.classList.add('active');
        positionResultZoom();
      }, 200);
    });
    thumbWrap.addEventListener('mousemove', function (event) {
      clearTimeout(closeTimer);
      positionResultZoom(event);
    });
    // 2026-08-28 定案：鼠标移入放大浮层本身不关闭——只查图细节时鼠标自然
    // 滑向大图，旧版缩略图 mouseleave 即关浮层把人挡在半路。改为"离开缩略图
    // 且不在浮层内"才关：浮层 mouseleave 兜底 + 离开缩略图时若指针在浮层内
    // 推迟一帧再判定。
    resultZoomLayer.addEventListener('mouseenter', function () { isPointerOverZoomLayer = true; clearTimeout(closeTimer); });
    resultZoomLayer.addEventListener('mouseleave', function () {
      isPointerOverZoomLayer = false;
      hideResultZoom();
    });
    thumbWrap.addEventListener('mouseleave', function () {
      clearTimeout(hoverTimer);
      // 指针可能正移向浮层（两元素间有一段空隙/重叠）——短暂宽限后再判
      closeTimer = setTimeout(function () {
        if (!isPointerOverZoomLayer) hideResultZoom();
      }, 120);
    });
  }

  function positionResultZoom(event) {
    if (!resultZoomLayer.classList.contains('active')) return;
    var pointer = event || window.event;
    var viewportWidth = window.innerWidth;
    var viewportHeight = window.innerHeight;
    // 浮层尺寸按图片自然尺寸钳制（首帧未加载完成时用当前显示尺寸）
    var naturalWidth = resultZoomImage.naturalWidth || ZOOM_VIEW_MAX;
    var naturalHeight = resultZoomImage.naturalHeight || ZOOM_VIEW_MAX;
    var scale = Math.min(1, ZOOM_VIEW_MAX / Math.max(naturalWidth, naturalHeight));
    var viewWidth = Math.max(120, Math.round(naturalWidth * scale));
    var viewHeight = Math.max(120, Math.round(naturalHeight * scale));
    resultZoomImage.style.width = viewWidth + 'px';
    resultZoomImage.style.height = viewHeight + 'px';
    // 位置：鼠标右下方，越界翻转到左/上
    var pointerX = pointer && typeof pointer.clientX === 'number' ? pointer.clientX : viewportWidth / 2;
    var pointerY = pointer && typeof pointer.clientY === 'number' ? pointer.clientY : viewportHeight / 2;
    var left = pointerX + 24;
    var top = pointerY + 24;
    if (left + viewWidth + ZOOM_VIEW_MARGIN > viewportWidth) left = Math.max(ZOOM_VIEW_MARGIN, pointerX - viewWidth - 24);
    if (top + viewHeight + ZOOM_VIEW_MARGIN > viewportHeight) top = Math.max(ZOOM_VIEW_MARGIN, pointerY - viewHeight - 24);
    resultZoomLayer.style.left = left + 'px';
    resultZoomLayer.style.top = top + 'px';
  }

  function hideResultZoom() {
    if (resultZoomLayer) resultZoomLayer.classList.remove('active');
  }

  // ---- 免责声明与限制（GET /api/meta，11 号验收） ----

  async function loadMeta() {
    try {
      var response = await fetch('/api/meta');
      if (!response.ok) return;
      var meta = await response.json();
      var disclaimerElement = document.getElementById('disclaimer-text');
      // ttl-hint 子元素是常驻行（24h 删除），textContent 赋值会冲掉——先摘再挂
      var ttlHint = disclaimerElement.querySelector('.ttl-hint');
      disclaimerElement.textContent = meta.disclaimer + (meta.remote_api_disclaimer || '');
      if (ttlHint) disclaimerElement.appendChild(ttlHint);
      document.getElementById('upload-limits-hint').textContent =
        '支持 PNG / JPG / WebP · 最多 ' + meta.max_images + ' 张 · 单张 ≤ ' + meta.max_image_mb + 'MB'
        + (meta.min_image_pixels ? ' · 最小边 ≥ ' + meta.min_image_pixels + 'px' : '')
        + (meta.reject_duplicate_images ? ' · 请勿重复上传相同图片' : '');
    } catch (metaError) { /* 兜底文案已写死在 HTML */ }
  }

  // 右栏常驻初始态（2026-08-27 双栏：未提交时引导文案占位，不再隐藏面板）
  // 2026-08-29：容器改 grid 水平垂直双向居中——占位文案始终在卡片正中
  // （旧版 padding-top 挤压式"伪居中"，面板高度变化时文案偏上）
  function showResultPlaceholder() {
    resultList.style.display = 'grid';
    resultList.style.placeItems = 'center';
    resultList.innerHTML = '<div class="status-text" style="padding:12px;text-align:center;color:#b0b0b0;">'
      + '处理结果将显示在这里<br>左侧选择图片 → 点击「开始处理」</div>';
  }
  showResultPlaceholder();

  loadMeta();
})();
