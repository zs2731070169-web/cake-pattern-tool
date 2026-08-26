/* pattern-tool 前端逻辑：上传预压 → 可选裁剪 → 提交轮询 → 分端交付。
   技术方案 7.1：无构建链原生 JS；预压用 canvas；Web Share / ClipboardItem 特性检测降级。 */
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
  var resultPanel = document.getElementById('result-panel');
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
      if (item.cropMeta && item.cropMeta.data && item.cropMeta.data.width > 0) {
        // 声明式裁剪回显（2026-08-26）：缩略图按声明现画形状预览（框裁+形状遮罩，
        // 形状外透明→thumb-wrap 棋盘格底）——仅展示层，提交仍是原图+声明
        attachCroppedPreview(thumb, item, itemIndex);
      }
      thumbWrap.appendChild(seqBadge);
      thumbWrap.appendChild(thumb);

      var info = document.createElement('div');
      info.className = 'card-info';
      var infoLine = document.createElement('div');
      infoLine.textContent = formatBytes(item.file.size) + (item.cropMeta ? ' · 已裁剪' : '');
      info.appendChild(infoLine);
      if (item.cropMeta && item.cropMeta.shape) {
        var shapeTag = document.createElement('div');
        shapeTag.className = 'tag done';
        shapeTag.textContent = '形状: ' + (SHAPE_LABELS[item.cropMeta.shape] || item.cropMeta.shape);
        info.appendChild(shapeTag);
      }
      // 尺寸选择（2026-08-26 新增，逐图独立；默认不选=原幅交付）
      var sizeRow = document.createElement('div');
      sizeRow.className = 'size-row';
      var sizeSelect = document.createElement('select');
      sizeSelect.className = 'size-select';
      var defaultOption = document.createElement('option');
      defaultOption.value = '';
      defaultOption.textContent = '打印尺寸：原幅';
      sizeSelect.appendChild(defaultOption);
      SIZE_OPTIONS.forEach(function (opt) {
        var option = document.createElement('option');
        option.value = String(opt.cm);
        option.textContent = opt.label;
        sizeSelect.appendChild(option);
      });
      var customOption = document.createElement('option');
      customOption.value = 'custom';
      customOption.textContent = '自定义 (cm)';
      sizeSelect.appendChild(customOption);
      sizeSelect.value = item.sizeCm ? String(item.sizeCm) : '';
      sizeSelect.addEventListener('change', function () {
        if (sizeSelect.value === 'custom') {
          var input = window.prompt('输入打印尺寸（厘米，5-100）', item.sizeCm || '15');
          var parsed = parseFloat(input);
          if (input != null && !isNaN(parsed) && parsed >= 5 && parsed <= 100) {
            item.sizeCm = parsed;
            sizeSelect.value = 'custom';
          } else {
            sizeSelect.value = item.sizeCm ? 'custom' : '';
            if (input != null) window.alert('请输入 5-100 之间的数字');
          }
        } else if (sizeSelect.value === '') {
          item.sizeCm = null;
        } else {
          item.sizeCm = parseFloat(sizeSelect.value);
        }
      });
      sizeRow.appendChild(sizeSelect);
      info.appendChild(sizeRow);

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
        sizeCm: null,
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

  function openCropModal(itemIndex) {
    cropTargetIndex = itemIndex;
    // 形状重置为默认自由矩形（2026-08-26 需求：每次打开都从默认开始，
    // 不停留上一次选择——避免客户对多张图连续裁剪时误用前一张的形状）
    cropShapeSelect.value = 'rectangle';
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
      if (meta.shape !== 'rectangle' && meta.shape !== 'rectangle-fixed' && meta.shape !== 'square') {
        // 形状遮罩：clip 后画图，形状外保持透明（棋盘格底透出形状观感）
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
      // 每图必有形状（2026-08-25 需求；2026-08-27 默认值修订）：未显式裁剪
      // 的图默认=矩形整图（不裁切不透明化，描边沿整图边框）——原默认圆形
      // 撤销（用户实测"没选形状被裁成圆"非预期）
      // 尺寸声明（2026-08-26）：选了打印尺寸时随 crop_meta 附 size.cm
      var seqMeta = item.cropMeta || { shape: 'rectangle', default: true, box: null };
      if (item.sizeCm) seqMeta.size = { cm: item.sizeCm };
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
      resultPanel.style.display = '';
      resultList.innerHTML = '<div class="status-text processing">处理中，请稍候…</div>';
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
    pollJobStatus(); // 立即首查
  }

  function stopPolling() {
    if (pollTimerId) { clearInterval(pollTimerId); pollTimerId = null; }
  }

  async function pollJobStatus() {
    if (!submittingJobId) return;
    try {
      var response = await fetch('/api/jobs/' + submittingJobId);
      if (response.status === 404) {
        stopPolling();
        resultList.innerHTML = '<div class="status-text failed">批次已过期，请重新上传</div>';
        return;
      }
      if (!response.ok) return; // 瞬时错误：下轮重试
      var jobStatus = await response.json();
      renderResults(jobStatus);
      if (jobStatus.status === 'completed') stopPolling();
    } catch (networkError) { /* 网络抖动下轮重试 */ }
  }

  // ---- 结果渲染与分端交付 ----

  // 全部执行步骤展示（2026-08-26 补裁剪/缩放）；crop 记档是声明 JSON 特判
  // 执行顺序（垂直排列）：去水印→填充→裁剪→描边→分辨率（2026-08-26 21:54
  // 补 resize 格；值=实际分辨率，异步从结果图回填，回填前显示状态词）
  var STAGE_LABELS = { watermark: '去水印', fill: '填充', crop: '裁剪', outline: '描边', resize: '分辨率' };
  var STAGE_VALUE_LABELS = { 'done': '已处理', 'skipped': '跳过', 'fallback': '保留原样', 'done(api)': 'AI处理', 'done(degraded)': 'AI降级', '白色背景': '白底替换', 'done(upscaled)': '已自动提升' };
  var SHAPE_CN = { circle: '圆形', square: '正方形', rectangle: '自由矩形', 'rectangle-fixed': '长方形', heart: '爱心', star: '星形', free: '自由矩形' };
  // low-res 提示已撤（2026-08-26 16:22 定案：低清图自动变清晰提升，不再提醒换图）
  var HINT_LABELS = { 'heavy-watermark': '水印较复杂，建议人工复核' };

  var batchDownloadButton = document.getElementById('batch-download-button');
  var batchStatus = document.getElementById('batch-status');
  var batchResultUrls = []; // 当前批次的 completed 结果 URL 序列（批量动作用）

  function renderResults(jobStatus) {
    resultList.innerHTML = '';
    batchResultUrls = [];
    jobStatus.images.forEach(function (imageStatus) {
      resultList.appendChild(buildResultCard(imageStatus));
      if (imageStatus.status === 'completed' && imageStatus.result_url) {
        batchResultUrls.push({ seq: imageStatus.seq, url: imageStatus.result_url });
      }
    });
    updateBatchActions();
  }

  function updateBatchActions() {
    var batchContainer = document.getElementById('batch-actions');
    var multi = batchResultUrls.length >= 1;
    batchContainer.style.display = multi ? 'flex' : 'none';
    // 多张时的主路径引导（2026-08-27）：批量复制已撤（剪贴板一次只能持
    // 1 张图，逐张倒计时体验鸡肋）——"一次发多张"走文件级路径：ZIP 下载
    // → 解压 → 全选拖入聊天窗口（聊天端多文件一次接收）
    if (batchStatus) {
      batchStatus.textContent = batchResultUrls.length >= 2
        ? '多张一次发窗口：批量下载 → 解压 → 全选拖入聊天窗口'
        : '';
    }
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
          var normalizedValue = stageValue === 'done' ? 'done' : (stageValue === 'skipped' ? 'skipped' : (stageValue === 'fallback' ? 'fallback' : 'done'));
          tag.className = 'tag ' + normalizedValue;
          tag.textContent = STAGE_LABELS[stageKey] + ':' + (STAGE_VALUE_LABELS[stageValue] || stageValue);
          if (stageKey === 'resize' && stageValue !== 'skipped') {
            // 值=实际分辨率：等缩略图加载完读 naturalWidth×Height 回填（图是同一张）
            resizeTagToFill = tag;
          }
        }
        stageTags.appendChild(tag);
      });
      info.appendChild(stageTags);

      if (imageStatus.quality_hint && imageStatus.quality_hint !== 'none') {
        var hintTag = document.createElement('span');
        hintTag.className = 'tag hint-' + imageStatus.quality_hint;
        hintTag.textContent = HINT_LABELS[imageStatus.quality_hint] || imageStatus.quality_hint;
        info.appendChild(hintTag);
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

      // 分端交付动作（10b：特性检测降级，基础动作不阻塞）
      var actions = document.createElement('div');
      actions.className = 'result-actions';
      actions.style.padding = '8px';

      if (isMobileEnvironment()) {
        // 移动端主路径：内联展示 + 长按存相册（系统能力，无需按钮）
        var longPressHint = document.createElement('div');
        longPressHint.className = 'mobile-hint';
        longPressHint.textContent = '长按上方图片保存到相册';
        actions.appendChild(longPressHint);
        if (navigator.share) {
          var shareButton = document.createElement('button');
          shareButton.textContent = '分享';
          shareButton.addEventListener('click', async function () {
            try {
              var imageResponse = await fetch(imageStatus.result_url);
              var imageBlob = await imageResponse.blob();
              var shareFile = new File([imageBlob], 'pattern.png', { type: 'image/png' });
              if (navigator.canShare && navigator.canShare({ files: [shareFile] })) {
                await navigator.share({ files: [shareFile], title: '修图结果' });
              } else {
                await navigator.share({ title: '修图结果', url: location.origin + imageStatus.result_url });
              }
            } catch (shareError) { /* 用户取消等：静默 */ }
          });
          actions.appendChild(shareButton);
        }
      } else {
        // 桌面端主路径：下载落默认目录
        var downloadLink = document.createElement('a');
        downloadLink.className = 'download-link';
        downloadLink.href = imageStatus.result_url;
        downloadLink.download = 'pattern_' + imageStatus.seq + '.png';
        downloadLink.textContent = '下载';
        actions.appendChild(downloadLink);
        if (navigator.clipboard && window.ClipboardItem) {
          var copyButton = document.createElement('button');
          copyButton.textContent = '复制图片';
          copyButton.addEventListener('click', async function () {
            try {
              var clipboardResponse = await fetch(imageStatus.result_url);
              var clipboardBlob = await clipboardResponse.blob();
              await navigator.clipboard.write([new ClipboardItem({ 'image/png': clipboardBlob })]);
              copyButton.textContent = '已复制';
            } catch (clipboardError) {
              copyButton.textContent = '复制失败';
            }
          });
          actions.appendChild(copyButton);
        }
      }
      card.appendChild(info);
      card.appendChild(actions);
    } else {
      card.appendChild(info);
    }

    card.insertBefore(thumbWrap, card.firstChild);
    return card;
  }

  restartButton.addEventListener('click', function () {
    stopPolling();
    submittingJobId = null;
    resultPanel.style.display = 'none';
    hideResultZoom();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  // ---- 结果图悬停放大（桌面 hover 查看细节；触屏无 hover 不触发，pointer-events:none 不打断点击/长按） ----

  var resultZoomLayer = document.getElementById('result-zoom-layer');
  var resultZoomImage = resultZoomLayer ? resultZoomLayer.querySelector('img') : null;
  var ZOOM_VIEW_MAX = 480; // 浮层最大边（px），避免大图铺满屏
  var ZOOM_VIEW_MARGIN = 16; // 距视口边距

  function attachResultZoom(thumbWrap, imageUrl) {
    if (!resultZoomLayer || !resultZoomImage) return;
    thumbWrap.classList.add('zoomable');
    thumbWrap.addEventListener('mouseenter', function () {
      if (resultZoomImage.getAttribute('src') !== imageUrl) resultZoomImage.src = imageUrl;
      resultZoomLayer.classList.add('active');
      positionResultZoom();
    });
    thumbWrap.addEventListener('mousemove', positionResultZoom);
    thumbWrap.addEventListener('mouseleave', hideResultZoom);
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
      document.getElementById('disclaimer-text').textContent = meta.disclaimer + (meta.remote_api_disclaimer || '');
      document.getElementById('upload-limits-hint').textContent =
        '支持 PNG / JPG / WebP · 最多 ' + meta.max_images + ' 张 · 单张 ≤ ' + meta.max_image_mb + 'MB'
        + (meta.min_image_pixels ? ' · 最小边 ≥ ' + meta.min_image_pixels + 'px' : '')
        + (meta.reject_duplicate_images ? ' · 请勿重复上传相同图片' : '');
    } catch (metaError) { /* 兜底文案已写死在 HTML */ }
  }

  loadMeta();
})();
