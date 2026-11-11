/**
 * Super League screenshot bridge.
 * Images are staged first; the currently published image set is kept until
 * every new image has been decoded, validated and inserted successfully.
 */
const ACTIVE_IMAGE_PREFIX = 'SUPER_LEAGUE_SEASON:';
const PENDING_IMAGE_PREFIX = 'SUPER_LEAGUE_PENDING:';
const MAX_IMAGE_BYTES = 750000; // Keep below SpreadsheetApp Sheet.insertImage's 2 MB limit.
const REQUIRED_IMAGE_KEYS = ['cup_bracket', 'c1_playin', 'c1_groups', 'c1_knockout'];

function setupSyncToken() {
  var token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty('SUPER_LEAGUE_SYNC_TOKEN', token);
  Logger.log('SUPER_LEAGUE_SYNC_TOKEN=' + token);
  return token;
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;
  var stagedImages = [];
  var committed = false;
  var batchId = Utilities.getUuid().replace(/-/g, '');
  var sheetTitle = '';

  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error('Thiếu request body.');
    var payload = JSON.parse(e.postData.contents);
    var expectedToken = PropertiesService.getScriptProperties().getProperty('SUPER_LEAGUE_SYNC_TOKEN');
    if (!expectedToken || payload.token !== expectedToken) {
      return jsonOutput({ ok: false, error: 'Token không hợp lệ.' });
    }
    if (payload.mode === 'season') {
      if (!payload.spreadsheet_id) throw new Error('Payload thiếu spreadsheet_id.');
      lock.waitLock(30000);
      lockAcquired = true;
      return handleSeasonPayload_(payload);
    }
    if (payload.mode === 'season_history') {
      if (!payload.spreadsheet_id) throw new Error('Payload thiếu spreadsheet_id.');
      lock.waitLock(30000);
      lockAcquired = true;
      return handleSeasonHistoryPayload_(payload);
    }
    if (!payload.spreadsheet_id || !payload.sheet_title || !Array.isArray(payload.images)) {
      throw new Error('Payload thiếu spreadsheet_id, sheet_title hoặc images.');
    }
    sheetTitle = String(payload.sheet_title);

    // Decode and validate the complete set before touching the spreadsheet.
    var preparedImages = validateAndDecodeImages(payload.images);

    // Avoid two syncs interleaving their stage/commit/cleanup phases.
    lock.waitLock(30000);
    lockAcquired = true;

    var spreadsheet = SpreadsheetApp.openById(String(payload.spreadsheet_id));
    var sheet = spreadsheet.getSheetByName(sheetTitle);
    if (!sheet) throw new Error('Không tìm thấy worksheet: ' + sheetTitle);

    // Phase 1: insert every new image under a temporary tag. Old images remain.
    preparedImages.forEach(function(item) {
      var blob = Utilities.newBlob(item.bytes, item.mime_type, item.filename);
      var image = sheet.insertImage(blob, 1, item.row);
      stagedImages.push({ image: image, key: item.key });
      image.setAltTextTitle(PENDING_IMAGE_PREFIX + batchId + ':' + item.key);
      image.setAltTextDescription(item.description || item.key);
      image.setWidth(item.display_width);
      image.setHeight(item.display_height);
      image.setAnchorCellXOffset(8);
      image.setAnchorCellYOffset(6);
      sheet.setRowHeight(item.row, item.display_height + 14);
    });

    // Phase 2: publish all staged images. If any promotion fails, the catch
    // block removes this batch and leaves the previous image set untouched.
    stagedImages.forEach(function(entry) {
      entry.image.setAltTextTitle(ACTIVE_IMAGE_PREFIX + batchId + ':' + entry.key);
    });
    committed = true;

    // Phase 3: only now remove older integration-owned images. Cleanup failures
    // do not roll back the new complete set; a later sync retries the cleanup.
    var cleanupErrors = 0;
    sheet.getImages().forEach(function(image) {
      try {
        var title = image.getAltTextTitle() || '';
        var isOwned = title.indexOf(ACTIVE_IMAGE_PREFIX) === 0 ||
                      title.indexOf(PENDING_IMAGE_PREFIX) === 0;
        var isCurrentBatch = title.indexOf(ACTIVE_IMAGE_PREFIX + batchId + ':') === 0;
        if (isOwned && !isCurrentBatch) image.remove();
      } catch (cleanupError) {
        cleanupErrors += 1;
      }
    });

    return jsonOutput({
      ok: true,
      image_count: stagedImages.length,
      sheet_title: sheetTitle,
      cleanup_pending: cleanupErrors > 0,
      cleanup_error_count: cleanupErrors
    });
  } catch (error) {
    // Roll back only images created by this attempt. Never delete the prior set
    // on a decode/insert/promotion failure.
    if (!committed) {
      stagedImages.forEach(function(entry) {
        try { entry.image.remove(); } catch (rollbackError) { /* Keep going. */ }
      });
      return jsonOutput({
        ok: false,
        error: String(error && error.message ? error.message : error),
        rolled_back_count: stagedImages.length,
        previous_images_preserved: true
      });
    }

    // The new complete set is already published. A late response-side error
    // must not report a destructive failure or remove the committed images.
    return jsonOutput({
      ok: true,
      image_count: stagedImages.length,
      sheet_title: sheetTitle,
      cleanup_pending: true,
      warning: String(error && error.message ? error.message : error)
    });
  } finally {
    if (lockAcquired) lock.releaseLock();
  }
}

function validateAndDecodeImages(images) {
  if (images.length !== REQUIRED_IMAGE_KEYS.length) {
    throw new Error('Cần gửi đúng ' + REQUIRED_IMAGE_KEYS.length + ' ảnh.');
  }
  var byKey = {};
  images.forEach(function(item) {
    if (!item || !item.key || !item.data_base64) throw new Error('Thiếu thông tin ảnh trong payload.');
    var key = String(item.key);
    if (REQUIRED_IMAGE_KEYS.indexOf(key) < 0) throw new Error('Key ảnh không hợp lệ: ' + key);
    if (byKey[key]) throw new Error('Ảnh bị trùng key: ' + key);
    var row = Number(item.row);
    if (!Number.isInteger(row) || row < 1) throw new Error('Vị trí hàng không hợp lệ cho ảnh ' + key + '.');
    var base64Data = String(item.data_base64);
    // Reject huge values before allocating the decoded byte array.
    if (base64Data.length > Math.ceil(MAX_IMAGE_BYTES / 3) * 4 + 8) {
      throw new Error('Ảnh ' + key + ' vượt giới hạn 750.000 byte.');
    }
    var mimeType = String(item.mime_type || 'image/jpeg').toLowerCase();
    if (mimeType !== 'image/jpeg' && mimeType !== 'image/png') {
      throw new Error('Định dạng ảnh không được hỗ trợ: ' + mimeType);
    }
    var bytes;
    try {
      bytes = Utilities.base64Decode(base64Data);
    } catch (decodeError) {
      throw new Error('Base64 của ảnh ' + key + ' không hợp lệ.');
    }
    if (!bytes || bytes.length === 0) throw new Error('Ảnh ' + key + ' không có dữ liệu.');
    if (bytes.length > MAX_IMAGE_BYTES) {
      throw new Error('Ảnh ' + key + ' có ' + bytes.length + ' byte, vượt giới hạn ' + MAX_IMAGE_BYTES + '.');
    }
    byKey[key] = {
      key: key,
      row: row,
      bytes: bytes,
      mime_type: mimeType,
      filename: String(item.filename || (key + (mimeType === 'image/png' ? '.png' : '.jpg'))),
      description: String(item.description || key),
      display_width: Math.max(320, Math.min(1000, Number(item.display_width || 1000))),
      display_height: Math.max(180, Math.min(760, Number(item.display_height || 560)))
    };
  });
  return REQUIRED_IMAGE_KEYS.map(function(key) {
    if (!byKey[key]) throw new Error('Thiếu ảnh bắt buộc: ' + key);
    return byKey[key];
  });
}

function jsonOutput(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

// ============================================================================
// BẢNG TỔNG HỢP MÙA GIẢI (ĐỘNG - dựng trực tiếp từ JSON lưu trữ mùa giải)
// ----------------------------------------------------------------------------
// Không có dữ liệu cố định trong code. Có 2 cách cấp dữ liệu:
//  1) App gửi POST: { token, spreadsheet_id, mode: "season", season: <JSON file mùa giải>,
//     sheet_title (tùy chọn) }. `season` có thể là object hoặc chuỗi JSON,
//     có `record` bọc ngoài hay không đều được.
//  2) Chạy tay: upload file JSON mùa giải lên Google Drive, mở Apps Script gắn với Sheet,
//     chạy hàm buildSeasonSheetFromDrive và dán File ID (lần sau tự nhớ).
// Tên sheet mặc định lấy theo nhãn mùa giải, ví dụ "Mùa giải 2026-2027".
// ============================================================================
const HEADER_FILL = '#1F4E78';
const BORDER_COLOR = '#BFBFBF';
const SEASON_FILE_PROP = 'SEASON_JSON_FILE_ID';

function setSeasonFileId(fileId) {
  PropertiesService.getScriptProperties().setProperty(SEASON_FILE_PROP, String(fileId));
}

function buildSeasonSheetFromDrive() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty(SEASON_FILE_PROP);
  if (!id) {
    var res = SpreadsheetApp.getUi().prompt('Dán File ID của file JSON mùa giải trên Google Drive:');
    if (res.getSelectedButton() !== SpreadsheetApp.getUi().Button.OK) return;
    id = res.getResponseText().trim();
    props.setProperty(SEASON_FILE_PROP, id);
  }
  var text = DriveApp.getFileById(id).getBlob().getDataAsString('UTF-8');
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Hãy gắn script vào Google Sheet (Extensions > Apps Script).');
  writeSeasonToSpreadsheet_(ss, text, '');
}

function writeSeasonToSpreadsheet_(spreadsheet, seasonInput, sheetTitle) {
  var rec = seasonRecord_(seasonInput);
  var table = buildSeasonTable_(rec);
  var name = String(sheetTitle || defaultSeasonSheetName_(rec));
  var sheet = spreadsheet.getSheetByName(name) || spreadsheet.insertSheet(name);
  renderSeasonTable_(sheet, table);
  return { sheet_title: name, row_count: table.rows.length };
}

function handleSeasonPayload_(payload) {
  if (!payload.season) throw new Error('Thiếu dữ liệu `season`.');
  var spreadsheet = SpreadsheetApp.openById(String(payload.spreadsheet_id));
  var r = writeSeasonToSpreadsheet_(spreadsheet, payload.season, payload.sheet_title);
  return jsonOutput({ ok: true, mode: 'season', sheet_title: r.sheet_title, row_count: r.row_count });
}


/**
 * Nhận lịch sử mùa giải do ứng dụng đọc từ SQLite và gửi qua Apps Script.
 *
 * Ứng dụng Super League đọc archive từ database SQLite rồi gửi:
 * {
 *   token, spreadsheet_id, mode: "season_history",
 *   seasons: [
 *     {"file_name":"2026-2027.json", "season": <archive JSON>},
 *     {"file_name":"2027-2028.json", "season": <archive JSON>}
 *   ]
 * }
 *
 * `season` có thể là archive chuẩn {format, record, ...} hoặc record thuần.
 * Mỗi mùa được ghi vào một worksheet riêng; chạy lại sẽ cập nhật worksheet
 * cùng tên, không tạo tab trùng.
 */
function handleSeasonHistoryPayload_(payload) {
  if (!Array.isArray(payload.seasons) || payload.seasons.length === 0) {
    throw new Error('Payload season_history cần có mảng seasons không rỗng.');
  }

  var spreadsheet = SpreadsheetApp.openById(String(payload.spreadsheet_id));
  var prepared = [];
  var seen = {};

  // Kiểm tra toàn bộ dữ liệu trước khi bắt đầu ghi để tránh ghi nửa chừng
  // do payload thiếu record hoặc trùng mùa.
  payload.seasons.forEach(function(item, index) {
    if (!item || !item.season) {
      throw new Error('Thiếu dữ liệu season tại phần tử #' + (index + 1) + '.');
    }
    var rec = seasonRecord_(item.season);
    var seasonNumber = Number(rec.season);
    if (!Number.isInteger(seasonNumber) || seasonNumber < 1900 || seasonNumber > 3000) {
      throw new Error('Năm mùa giải không hợp lệ tại phần tử #' + (index + 1) + '.');
    }
    if (seen[seasonNumber]) {
      throw new Error('Payload có nhiều file cho mùa ' + seasonNumber + '.');
    }
    seen[seasonNumber] = true;

    if (!Array.isArray(rec.matches)) {
      throw new Error('Mùa ' + (rec.label || seasonNumber) + ': trường matches phải là mảng.');
    }

    // Dựng bảng ngay trong bước kiểm tra để phát hiện lỗi cấu trúc trước khi ghi.
    var table = buildSeasonTable_(rec);
    if (!table || !table.rows || !table.rows.length) {
      throw new Error('Không tạo được bảng dữ liệu cho mùa ' + seasonNumber + '.');
    }
    prepared.push({
      season: seasonNumber,
      label: String(rec.label || (seasonNumber + '/' + (seasonNumber + 1))),
      requestedTitle: item.sheet_title || '',
      table: table
    });
  });

  prepared.sort(function(a, b) { return a.season - b.season; });

  var results = [];
  prepared.forEach(function(item) {
    var title = String(item.requestedTitle || ('Mùa giải ' + item.label))
      .replace(/[\/\\?*\[\]:]/g, '-')
      .substring(0, 99);
    var sheet = spreadsheet.getSheetByName(title) || spreadsheet.insertSheet(title);
    renderSeasonTable_(sheet, item.table);
    results.push({
      season: item.season,
      label: item.label,
      sheet_title: title,
      row_count: item.table.rows.length
    });
  });

  return jsonOutput({
    ok: true,
    mode: 'season_history',
    season_count: results.length,
    seasons: results
  });
}

function seasonRecord_(input) {
  var obj = (typeof input === 'string') ? JSON.parse(input) : input;
  if (!obj) throw new Error('Dữ liệu mùa giải rỗng.');
  return obj.record ? obj.record : obj;
}

function defaultSeasonSheetName_(rec) {
  var label = String(rec.label || rec.season || 'mùa giải').replace(/[\/\\?*\[\]:]/g, '-');
  return 'Mùa giải ' + label;
}

// ---- Dựng các dòng bảng từ record ------------------------------------------
// Loại dòng: T tiêu đề phần, S tiêu đề phụ, H đầu bảng, D dữ liệu, b dòng trống.
function buildSeasonRows_(r) {
  var rows = [];
  var M = {};
  (r.matches || []).forEach(function(m) { M[m.id] = m; });

  function clean(v) { return (v === null || v === undefined) ? '' : v; }
  function T(t) { rows.push(['T', [t]]); }
  function S(t) { rows.push(['S', [t]]); }
  function blank() { rows.push(['b', []]); }
  function table(header, data) {
    rows.push(['H', header.slice()]);
    data.forEach(function(x) { rows.push(['D', x.map(clean)]); });
    blank();
  }
  function endSection() { blank(); blank(); }
  function vals(o) { return Object.keys(o || {}).map(function(k) { return o[k]; }); }
  function sorted(list) { return (list || []).slice(); }

  function goalsStr(m) {
    return (m.goals || []).map(function(g) {
      return g.minute + "' " + g.scorer + ' (' + g.team + ')' + (g.pen ? ' (pen)' : '');
    }).join('; ');
  }
  function redsStr(m) {
    return (m.red_cards || []).map(function(c) {
      return c.minute + "' " + c.player + ' (' + c.team + ', ' + c.type + ')';
    }).join('; ');
  }
  var MH = ['ID', 'Ngày', 'Giải', 'Vòng', 'Chủ nhà', 'BT chủ', 'BT khách', 'Khách', 'Đội thắng', 'Ghi chú', 'Bàn thắng', 'Thẻ đỏ'];
  function mrow(id) {
    var m = M[id];
    if (!m) return [id, '', '', '', '', '', '', '', 'Không tìm thấy trận', '', '', ''];
    var note = [];
    if (m.aet) note.push('Hiệp phụ');
    if (m.pens) note.push('Pens ' + m.pens.home + '-' + m.pens.away);
    return [m.id, m.date, m.comp, m.round, m.home, m.hg, m.ag, m.away,
            m.winner || 'Hòa', note.join(', '), goalsStr(m), redsStr(m)];
  }
  var TH = ['Hạng', 'Đội', 'Trận', 'Thắng', 'Hòa', 'Thua', 'BT', 'BB', 'HS', 'Điểm'];
  function trow(i, t) { return [i, t.team, t.p, t.w, t.d, t.l, t.gf, t.ga, t.gd, t.pts]; }

  // 1. Tổng quan
  var a = r.competition_awards || {};
  var fc1 = a.fc1 || {}, fc2 = a.fc2 || {};
  T('TỔNG QUAN MÙA GIẢI');
  table(['Hạng mục', 'Giá trị'], [
    ['Mùa giải', r.label],
    ['Vô địch Premier League', r.champion_premier],
    ['Vô địch Cup', r.champion_cup],
    ['Á quân Cup', r.runner_up_cup],
    ['Vô địch C1', r.champion_c1],
    ['Á quân C1', r.runner_up_c1],
    ['Vô địch FC1', fc1.champion],
    ['Vô địch FC2', fc2.champion],
    ['Tổng số trận', r.match_count]
  ]);
  endSection();

  // 2. Giải thưởng theo giải đấu
  T('GIẢI THƯỞNG THEO GIẢI ĐẤU');
  table(['Giải', 'Vô địch', 'Á quân', 'Cầu thủ xuất sắc nhất', 'CLB', 'Vị trí', 'Trận', 'Bàn', 'Kiến tạo', 'Rating TB', 'MOTM', 'Ngày kết thúc'],
    vals(a).map(function(v) {
      var p = v.player_of_tournament || {};
      return [v.competition, v.champion, v.runner_up, p.player, p.team, p.position, p.apps, p.goals, p.assists, p.avg_rating, p.motm_count, v.completed_date];
    }));
  endSection();

  // 3. Giải thưởng cá nhân
  var sa = r.season_awards || {};
  T('GIẢI THƯỞNG CÁ NHÂN MÙA GIẢI');
  function prow(label, p) {
    p = p || {};
    return [label, p.player, p.team, p.position, p.apps, p.goals, p.assists, p.avg_rating, p.motm_count, p.age, p.vote_score];
  }
  table(['Giải', 'Cầu thủ', 'CLB', 'Vị trí', 'Trận', 'Bàn', 'Kiến tạo', 'Rating TB', 'MOTM', 'Tuổi', 'Điểm bầu chọn'], [
    prow('Quả bóng Vàng', sa.ballon_dor), prow('Quả bóng Bạc', sa.ballon_silver),
    prow('Quả bóng Đồng', sa.ballon_bronze), prow('Cầu thủ trẻ xuất sắc', sa.young_player)]);
  endSection();

  T('ĐỘI HÌNH TIÊU BIỂU 4-4-2');
  table(['Vai trò', 'Cầu thủ', 'CLB', 'Vị trí gốc', 'Trận', 'Bàn', 'Kiến tạo', 'Rating TB', 'MOTM', 'Điểm Best XI'],
    (sa.best_xi_442 || []).map(function(p) {
      return [p.role, p.player, p.team, p.position, p.apps, p.goals, p.assists, p.avg_rating, p.motm_count, p.best_xi_score];
    }));
  endSection();

  T('TOP 20 ỨNG VIÊN QUẢ BÓNG VÀNG');
  table(['Hạng', 'Cầu thủ', 'CLB', 'Vị trí', 'Trận', 'Bàn', 'Kiến tạo', 'Rating TB', 'MOTM', 'Điểm bầu chọn', 'Danh hiệu'],
    sorted(sa.top20_players).slice(0, 20).map(function(p) {
      return [p.rank, p.player, p.team, p.position, p.apps, p.goals, p.assists, p.avg_rating, p.motm_count, p.vote_score, p.ballon_award];
    }));
  endSection();

  // 4. Bảng xếp hạng các giải
  var lg = r.leagues || {};
  var lp2 = r.league_phase2 || {};
  T('BXH PREMIER LEAGUE (TỔNG GĐ1 + GĐ2)');
  table(TH.concat(['Điểm GĐ1', 'Điểm GĐ2', 'Nhóm']),
    (lg.premier || []).map(function(t, i) { return trow(i + 1, t).concat([t.phase1_pts, t.phase2_pts, t.group]); }));
  endSection();
  T('BXH PREMIER LEAGUE - GIAI ĐOẠN 1');
  table(TH, ((lp2.premier || {}).phase1_table || []).map(function(t, i) { return trow(i + 1, t); }));
  endSection();
  T('BXH FOOTBALL CHAMPIONSHIP 1');
  table(TH, (lg.fc1 || []).map(function(t, i) { return trow(i + 1, t); }));
  endSection();
  T('BXH FOOTBALL CHAMPIONSHIP 2');
  table(TH, (lg.fc2 || []).map(function(t, i) { return trow(i + 1, t); }));
  endSection();

  // 5. Play-off & thăng/xuống hạng
  T('PLAY-OFF THĂNG HẠNG, THĂNG / XUỐNG HẠNG');
  var po = lp2.promotion_playoffs || {};
  table(['Giải', 'Hạng 1', 'Hạng 2', 'Hạng 3', 'Hạng 4', 'Thắng play-off'],
    Object.keys(po).map(function(k) {
      var v = po[k], tm = v.teams || {};
      return [k.toUpperCase(), tm.first, tm.second, tm.third, tm.fourth, v.winner];
    }));
  table(['Loại', 'Đội', 'Từ', 'Đến', 'Lý do'],
    (r.promoted || []).map(function(t) { return ['Thăng hạng', t.team, t.from, t.to, t.path]; })
      .concat((r.relegated || []).map(function(t) { return ['Xuống hạng', t.team, t.from, t.to, t.path]; })));
  endSection();

  // 6. Nhánh Cup
  T('NHÁNH ĐẤU CUP');
  var cb = r.cup_bracket || {};
  [['r1', 'Vòng 1'], ['r16', 'Vòng 16 đội'], ['qf', 'Tứ kết'], ['sf', 'Bán kết'], ['final', 'Chung kết']].forEach(function(p) {
    S(p[1]);
    table(MH, (cb[p[0]] || []).map(mrow));
  });
  endSection();

  // 7. C1 Play-in
  var c1 = r.c1 || {};
  T('C1 PLAY-IN');
  var pi = c1.playin || {};
  Object.keys(pi).forEach(function(g) {
    var v = pi[g];
    S('Bảng ' + g + ': ' + (v.teams || []).join(', '));
    var ids = (v.r1 || []).concat([v.winner_match, v.loser_match, v.decider]).filter(function(x) { return x !== null && x !== undefined; });
    table(MH, ids.map(mrow));
    table(['Suất vào vòng bảng', 'Đội'], [['Thắng nhánh thắng', v.winner_qualifier], ['Thắng trận quyết định', v.decider_qualifier]]);
  });
  endSection();

  // 8. C1 vòng bảng
  T('C1 MAIN STAGE - BẢNG XẾP HẠNG');
  var gt = c1.group_tables || {};
  Object.keys(gt).forEach(function(g) {
    S('Bảng ' + g);
    table(TH, gt[g].map(function(t, i) { return trow(i + 1, t); }));
  });
  endSection();

  // 9. C1 knockout
  T('C1 KNOCKOUT');
  var kb = c1.bracket || {};
  var kids = (kb.semis || []).concat(kb.final !== undefined && kb.final !== null ? [kb.final] : []);
  table(MH, kids.map(mrow));
  table(['Vô địch C1', 'Á quân C1'], [[c1.champion, c1.runner_up]]);
  endSection();

  // 10. Xét suất C1
  T('BẢNG ĐIỂM XÉT SUẤT C1');
  table(['Hạng', 'Đội', 'Giải', 'Hạng giải', 'Điểm giải', 'Điểm Cup', 'Tổng', 'HS', 'Elo'],
    (r.qualification_table || []).map(function(t) {
      return [t.rank, t.team, t.tier, t.league_rank, t.league_points, t.cup_points, t.total, t.gd, t.elo];
    }));
  endSection();

  // 11-13. Top 20 cầu thủ
  var PL = ['Hạng', 'Cầu thủ', 'CLB', 'Vị trí', 'Bàn', 'Kiến tạo', 'Trận', 'Phút', 'Rating TB'];
  function pl(i, p) { return [i, p.player, p.team, p.position, p.goals, p.assists, p.apps, p.minutes, p.avg_rating]; }
  T('TOP 20 VUA PHÁ LƯỚI');
  table(PL, sorted(r.top_scorers).slice(0, 20).map(function(p, i) { return pl(i + 1, p); }));
  endSection();
  T('TOP 20 KIẾN TẠO');
  table(PL, sorted(r.top_assists).slice(0, 20).map(function(p, i) { return pl(i + 1, p); }));
  endSection();
  T('TOP 20 PHÁT TRIỂN OVR NHIỀU NHẤT');
  var dev = sorted(r.development).sort(function(x, y) {
    return ((y.delta || 0) - (x.delta || 0)) || ((y.potential || 0) - (x.potential || 0));
  }).slice(0, 20);
  table(['Hạng', 'Cầu thủ', 'CLB', 'Vị trí', 'OVR cũ', 'OVR mới', 'Thay đổi', 'Tuổi', 'Tiềm năng', 'Phút', 'Rating TB'],
    dev.map(function(p, i) {
      return [i + 1, p.player, p.team, p.position, p.old_ovr, p.new_ovr, p.delta, p.age, p.potential, p.minutes, p.avg_rating];
    }));
  endSection();

  // 14-15. Giải nghệ & trẻ
  T('CẦU THỦ GIẢI NGHỆ');
  table(['Cầu thủ', 'CLB', 'Vị trí', 'Tuổi', 'OVR đỉnh cao', 'Trận', 'Bàn', 'Kiến tạo'],
    (r.retired || []).map(function(p) { return [p.player_name, p.club, p.position, p.age, p.overall_peak, p.appearances, p.goals, p.assists]; }));
  endSection();
  T('CẦU THỦ TRẺ ĐƯỢC PROMOTE');
  table(['Cầu thủ', 'CLB', 'Vị trí', 'Tuổi', 'OVR', 'Tiềm năng', 'Thay thế'],
    (r.youth_promoted || []).map(function(p) { return [p.player, p.team, p.position, p.age, p.overall, p.potential, p.replaced_player_id]; }));
  endSection();

  return rows;
}

function buildSeasonTable_(rec) {
  var rows = buildSeasonRows_(rec);
  var cols = 1, widths = [];
  rows.forEach(function(r) {
    cols = Math.max(cols, r[1].length);
    if (r[0] === 'T') return;
    r[1].forEach(function(v, c) {
      if (v === '' || v === null || v === undefined) return;
      var L = String(v).length;
      if (typeof v === 'string' && L > 70) L = 70;
      widths[c] = Math.max(widths[c] || 8, Math.min(L + 2, 70));
    });
  });
  for (var c = 0; c < cols; c++) if (!widths[c]) widths[c] = 8;
  return { cols: cols, widths: widths, rows: rows };
}

// ---- Vẽ bảng lên sheet -----------------------------------------------------
function renderSeasonTable_(sheet, table) {
  var cols = table.cols, rows = table.rows, n = rows.length;
  if (!cols || !n) throw new Error('Dữ liệu bảng rỗng.');
  sheet.clear();
  if (sheet.getMaxRows() < n) sheet.insertRowsAfter(sheet.getMaxRows(), n - sheet.getMaxRows());
  if (sheet.getMaxColumns() < cols) sheet.insertColumnsAfter(sheet.getMaxColumns(), cols - sheet.getMaxColumns());

  var values = [], formats = [];
  rows.forEach(function(r) {
    var v = r[1], rv = [], rf = [];
    for (var c = 0; c < cols; c++) {
      var x = c < v.length ? v[c] : '';
      rv.push(x);
      rf.push(typeof x === 'string' ? '@' : 'General'); // giữ nguyên chuỗi như "2026/2027", "2026-08-15"
    }
    values.push(rv); formats.push(rf);
  });
  var all = sheet.getRange(1, 1, n, cols);
  all.setNumberFormats(formats);
  all.setValues(values);
  all.setVerticalAlignment('top');

  var border = function(rg) {
    rg.setBorder(true, true, true, true, true, true, BORDER_COLOR, SpreadsheetApp.BorderStyle.SOLID);
  };
  rows.forEach(function(r, i) {
    var t = r[0], len = r[1].length, row = i + 1;
    if (t === 'T') {
      sheet.getRange(row, 1).setFontWeight('bold').setFontSize(13);
    } else if (t === 'S') {
      sheet.getRange(row, 1).setFontWeight('bold').setFontColor(HEADER_FILL);
    } else if (t === 'H') {
      var h = sheet.getRange(row, 1, 1, len);
      h.setBackground(HEADER_FILL).setFontColor('#FFFFFF').setFontWeight('bold')
       .setHorizontalAlignment('center').setVerticalAlignment('middle').setWrap(true);
      border(h);
    } else if (t === 'D') {
      var d = sheet.getRange(row, 1, 1, len);
      d.setWrap(true).setVerticalAlignment('top');
      border(d);
    }
  });

  (table.widths || []).forEach(function(w, c) {
    sheet.setColumnWidth(c + 1, Math.round(w * 7 + 5));
  });
  sheet.setHiddenGridlines(true);
  SpreadsheetApp.flush();
  sheet.autoResizeRows(1, n);
}