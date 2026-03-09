// ============================================================
// Run this as a Background Script on Sandbox
// Populates the EXISTING Update Set with 1 Script Include + 9 Properties
// Uses GlideUpdateManager2.saveRecord() for proper XML serialization
// ============================================================

(function() {
    var US_SYS_ID = '631608ce93dbb214dd4f32cdfaba10fb';

    var records = [
        { table: 'sys_script_include', sys_id: '9e391a0f878cbe909efa7488cebb352b', label: 'RefFieldFetcher' },
        { table: 'sys_properties', sys_id: '55f9da4f878cbe909efa7488cebb3590', label: 'allow_tables' },
        { table: 'sys_properties', sys_id: '56f95e4f878cbe909efa7488cebb3564', label: 'max_fields' },
        { table: 'sys_properties', sys_id: '7c0a128f878cbe909efa7488cebb3549', label: 'max_dot_depth' },
        { table: 'sys_properties', sys_id: '900ade4f878cbe909efa7488cebb35f2', label: 'deny_field_types' },
        { table: 'sys_properties', sys_id: 'b7f9de4f878cbe909efa7488cebb352b', label: 'deny_fields' },
        { table: 'sys_properties', sys_id: 'b9f95e4f878cbe909efa7488cebb350a', label: 'max_records' },
        { table: 'sys_properties', sys_id: 'dff99e4f878cbe909efa7488cebb358d', label: 'allow_acl_bypass' },
        { table: 'sys_properties', sys_id: 'ecf9da4f878cbe909efa7488cebb353d', label: 'enabled' },
        { table: 'sys_properties', sys_id: 'fef99e4f878cbe909efa7488cebb3535', label: 'cache_ttl_seconds' }
    ];

    // 1. Verify Update Set exists and is in progress
    var us = new GlideRecord('sys_update_set');
    if (!us.get(US_SYS_ID)) {
        gs.error('Update Set not found: ' + US_SYS_ID);
        return;
    }
    if (us.state != 'in progress') {
        gs.error('Update Set is not in progress (state=' + us.state + '). Set it to "in progress" first.');
        return;
    }
    gs.info('Target Update Set: ' + us.name + ' [' + US_SYS_ID + ']');

    // 2. Save the current update set preference
    var originalUS = gs.getPreference('sys_update_set');

    // 3. Set the target Update Set as current
    gs.getUser().savePreference('sys_update_set', US_SYS_ID);
    gs.info('Switched current Update Set to: ' + US_SYS_ID);

    // 4. Use GlideUpdateManager2.saveRecord() — the CORRECT way to serialize
    //    a record's full XML payload into sys_update_xml
    var um = new GlideUpdateManager2();
    var count = 0;
    for (var i = 0; i < records.length; i++) {
        var rec = records[i];
        var gr = new GlideRecord(rec.table);
        if (gr.get(rec.sys_id)) {
            um.saveRecord(gr);
            count++;
            gs.info('  Added: ' + rec.table + ' / ' + rec.label);
        } else {
            gs.warn('  NOT FOUND: ' + rec.table + ' / ' + rec.sys_id + ' (' + rec.label + ')');
        }
    }

    // 5. Complete the Update Set
    us.get(US_SYS_ID);
    us.state = 'complete';
    us.update();

    // 6. Restore original Update Set preference
    if (originalUS) {
        gs.getUser().savePreference('sys_update_set', originalUS);
    }

    gs.info('=== DONE === ' + count + ' records added to Update Set.');
    gs.info('View: /nav_to.do?uri=sys_update_set.do?sys_id=' + US_SYS_ID);
})();
