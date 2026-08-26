/* storezip.js — 极简 STORE 型 ZIP 打包器（无压缩，2026-08-26 内嵌）
   适用：PNG 等已压缩内容（deflate 无收益反费 CPU）。无构建链项目专用。
   API: StoreZip.builder().add(name, uint8array).build() → Blob (application/zip)
   实现：local file header + central directory + EOCD，CRC-32 查表。 */
(function (global) {
  'use strict';

  var CRC_TABLE = (function () {
    var table = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    var crc = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) {
      crc = (crc >>> 8) ^ CRC_TABLE[(crc ^ bytes[i]) & 0xFF];
    }
    return (crc ^ 0xFFFFFFFF) >>> 0;
  }

  function dosDateTime(date) {
    var d = date || new Date();
    var time = ((d.getHours() & 31) << 11) | ((d.getMinutes() & 63) << 5) | ((d.getSeconds() / 2) & 31);
    var day = (((d.getFullYear() - 1980) & 127) << 9) | (((d.getMonth() + 1) & 15) << 5) | (d.getDate() & 31);
    return { time: time, day: day };
  }

  function Writer() { this.parts = []; this.length = 0; }
  Writer.prototype.push = function (array) { this.parts.push(array); this.length += array.length; };
  Writer.prototype.blob = function (type) { return new Blob(this.parts, { type: type }); };

  function u16(v) { return new Uint8Array([v & 255, (v >>> 8) & 255]); }
  function u32(v) {
    return new Uint8Array([v & 255, (v >>> 8) & 255, (v >>> 16) & 255, (v >>> 24) & 255]);
  }

  function Builder() { this.entries = []; }

  Builder.prototype.add = function (name, data) {
    this.entries.push({ name: new TextEncoder().encode(name), data: data });
    return this;
  };

  Builder.prototype.build = function () {
    var out = new Writer();
    var central = new Writer();
    var stamp = dosDateTime(new Date());

    this.entries.forEach(function (entry) {
      var crc = crc32(entry.data);
      var offset = out.length;

      // local file header（固定 30 字节 + 文件名）
      out.push(u32(0x04034b50)); // signature
      out.push(u16(20)); // version needed
      out.push(u16(0x0800)); // flags: UTF-8 名
      out.push(u16(0)); // method: store
      out.push(u16(stamp.time));
      out.push(u16(stamp.day));
      out.push(u32(crc));
      out.push(u32(entry.data.length)); // compressed
      out.push(u32(entry.data.length)); // uncompressed
      out.push(u16(entry.name.length));
      out.push(u16(0)); // extra len
      out.push(entry.name);
      out.push(entry.data);

      // central directory 记录
      central.push(u32(0x02014b50));
      central.push(u16(20)); // version made by
      central.push(u16(20)); // version needed
      central.push(u16(0x0800));
      central.push(u16(0));
      central.push(u16(stamp.time));
      central.push(u16(stamp.day));
      central.push(u32(crc));
      central.push(u32(entry.data.length));
      central.push(u32(entry.data.length));
      central.push(u16(entry.name.length));
      central.push(u16(0)); // extra
      central.push(u16(0)); // comment
      central.push(u16(0)); // disk
      central.push(u16(0)); // internal attrs
      central.push(u32(0)); // external attrs
      central.push(u32(offset));
      central.push(entry.name);
    });

    // central directory 跟在全部 local 记录后；EOCD 收尾
    var centralOffset = out.length;
    var parts = out.parts.concat(central.parts);
    var tail = new Writer();
    tail.push(u32(0x06054b50)); // EOCD
    tail.push(u16(0));
    tail.push(u16(0));
    tail.push(u16(this.entries.length));
    tail.push(u16(this.entries.length));
    tail.push(u32(central.length));
    tail.push(u32(centralOffset));
    tail.push(u16(0));

    return new Blob(parts.concat(tail.parts), { type: 'application/zip' });
  };

  global.StoreZip = { builder: function () { return new Builder(); } };
})(window);
