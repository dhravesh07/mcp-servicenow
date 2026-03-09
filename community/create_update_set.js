// ============================================================
// Run this as a Background Script on Sandbox
// Creates "RefFieldFetcher v1.0 - Community Release" Update Set
// with 1 Script Include + 9 System Properties (properly serialized)
// ============================================================

(function() {
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

    // 1. Create Update Set
    var us = new GlideRecord('sys_update_set');
    us.initialize();
    us.name = 'RefFieldFetcher v1.0 - Community Release';
    us.description = 'Generic async GlideAjax endpoint for client scripts.\n1 Script Include (RefFieldFetcher) + 9 System Properties (ref_field_fetcher.*).\nSafe to install on any ServiceNow instance.';
    us.state = 'in progress';
    us.application = 'global';
    var usId = us.insert();
    gs.info('Created Update Set: ' + usId);

    // 2. Save the current update set preference (so we can restore it later)
    var originalUS = gs.getPreference('sys_update_set');

    // 3. Set our new Update Set as current
    gs.getUser().savePreference('sys_update_set', usId);
    gs.info('Set current Update Set to: ' + usId);

    // 4. Use GlideUpdateManager2.saveRecord() to properly serialize each
    //    record's full XML payload into sys_update_xml.
    //    NOTE: gr.update() does NOT work — it makes a no-op update that
    //    skips serialization. GlideUpdateManager2 is the correct API.
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
    us.get(usId);
    us.state = 'complete';
    us.update();

    // 6. Restore original Update Set preference
    if (originalUS) {
        gs.getUser().savePreference('sys_update_set', originalUS);
    }

    gs.info('=== DONE === Update Set "RefFieldFetcher v1.0 - Community Release" completed with ' + count + ' records.');
    gs.info('Export URL: /nav_to.do?uri=sys_update_set.do?sys_id=' + usId);
})();
