(function() {
  data.sysUserID = gs.getUser().getID();
  data.isAdmin = gs.hasRole('admin');
  if (!data.isAdmin) return;

  // ── Load existing categories for dropdown ──
  data.allCategories = [];
  var catGR = new GlideRecord('sys_choice');
  catGR.addQuery('name', 'incident');
  catGR.addQuery('element', 'category');
  catGR.addQuery('inactive', false);
  catGR.orderBy('label');
  catGR.query();
  while (catGR.next()) {
    data.allCategories.push({
      value: catGR.getValue('value'),
      label: catGR.getDisplayValue('label')
    });
  }

  if (!input) return;

  // ── Preserve client-side state through the server round-trip ──
  // c.server.update() replaces c.data with server's data object,
  // so we must copy input fields back to data to keep them alive.
  var _keep = ['categoryMode', 'selectedCategory', 'newCategoryLabel',
               'subcategories', 'closeCodes', 'categoryValue', 'categoryLabel',
               'isNewCategory', 'submitting', 'progress', 'hierarchy', 'summary',
               'action'];
  for (var _k = 0; _k < _keep.length; _k++) {
    if (input[_keep[_k]] !== undefined) {
      data[_keep[_k]] = input[_keep[_k]];
    }
  }

  // ── Shared helpers ──

  function choiceExists(element, value, dependentValue) {
    var gr = new GlideRecord('sys_choice');
    gr.addQuery('name', 'incident');
    gr.addQuery('element', element);
    gr.addQuery('value', value);
    if (dependentValue) gr.addQuery('dependent_value', dependentValue);
    gr.setLimit(1);
    gr.query();
    return gr.hasNext();
  }

  function parseLabels(text) {
    return text.split(/[\n,]+/)
      .map(function(s) { return s.replace(/^\s+|\s+$/g, ''); })
      .filter(function(s) { return s.length > 0; });
  }

  function derivePrefix(catVal) {
    var parts = catVal.split(' - ');
    return parts.length > 1
      ? parts.slice(1).join(' - ').toLowerCase()
      : parts[0].toLowerCase();
  }

  function loadHierarchy(catValue) {
    var catLabel = catValue;
    for (var i = 0; i < data.allCategories.length; i++) {
      if (data.allCategories[i].value === catValue) {
        catLabel = data.allCategories[i].label;
        break;
      }
    }
    var subcategories = [];
    var subGR = new GlideRecord('sys_choice');
    subGR.addQuery('name', 'incident');
    subGR.addQuery('element', 'subcategory');
    subGR.addQuery('dependent_value', catValue);
    subGR.addQuery('inactive', false);
    subGR.orderBy('label');
    subGR.query();
    while (subGR.next()) {
      var subValue = subGR.getValue('value');
      var closeCodes = [];
      var ccGR = new GlideRecord('sys_choice');
      ccGR.addQuery('name', 'incident');
      ccGR.addQuery('element', 'close_code');
      ccGR.addQuery('dependent_value', subValue);
      ccGR.addQuery('inactive', false);
      ccGR.orderBy('label');
      ccGR.query();
      while (ccGR.next()) {
        closeCodes.push({ value: ccGR.getValue('value'), label: ccGR.getDisplayValue('label') });
      }
      subcategories.push({ value: subValue, label: subGR.getDisplayValue('label'), closeCodes: closeCodes });
    }
    return { categoryLabel: catLabel, categoryValue: catValue, subcategories: subcategories };
  }

  // ── Action: load_hierarchy ──
  if (input.action === 'load_hierarchy') {
    if (input.categoryValue) data.hierarchy = loadHierarchy(input.categoryValue);
    return;
  }

  // ── Action: create_category ──
  if (input.action === 'create_category') {
    var categoryLabel = input.categoryLabel || '';
    var created = [];
    var skipped = [];
    var errors = [];

    if (!categoryLabel) {
      data.errors = ['No category name provided.'];
      data.stepSummary = 'No category name provided';
      return;
    }

    var categoryValue = categoryLabel.toLowerCase();
    if (choiceExists('category', categoryValue)) {
      skipped.push({ text: categoryLabel + ' (already exists)', type: 'category' });
      data.stepSummary = 'Category already exists';
    } else {
      var newCat = new GlideRecord('sys_choice');
      newCat.initialize();
      newCat.setValue('name', 'incident');
      newCat.setValue('element', 'category');
      newCat.setValue('value', categoryValue);
      newCat.setValue('label', categoryLabel);
      newCat.setValue('inactive', false);
      if (newCat.insert()) {
        created.push({ text: categoryLabel, detail: 'value: ' + categoryValue, type: 'category' });
        data.stepSummary = 'Created: ' + categoryLabel;
      } else {
        errors.push({ text: 'Failed to create: ' + categoryLabel, type: 'category' });
        data.stepSummary = 'Failed to create category';
      }
    }

    data.categoryValue = categoryValue;
    data.created = created;
    data.skipped = skipped;
    data.errors = errors;
    return;
  }

  // ── Action: create_subcategories ──
  if (input.action === 'create_subcategories') {
    var categoryValue = input.categoryValue || '';
    if (!categoryValue) { data.errors = ['No category value.']; return; }

    var prefix = derivePrefix(categoryValue);
    var subcats = parseLabels(input.subcategories || '');
    var created = [];
    var skipped = [];
    var errors = [];

    for (var i = 0; i < subcats.length; i++) {
      var subLabel = subcats[i];
      var subValue = prefix + ' - ' + subLabel.toLowerCase();

      if (choiceExists('subcategory', subValue, categoryValue)) {
        skipped.push({ text: subLabel, detail: subValue, type: 'subcategory' });
      } else {
        var subGR = new GlideRecord('sys_choice');
        subGR.initialize();
        subGR.setValue('name', 'incident');
        subGR.setValue('element', 'subcategory');
        subGR.setValue('value', subValue);
        subGR.setValue('label', subLabel);
        subGR.setValue('dependent_value', categoryValue);
        subGR.setValue('inactive', false);
        if (subGR.insert()) {
          created.push({ text: subLabel, detail: subValue, type: 'subcategory' });
        } else {
          errors.push({ text: 'Failed: ' + subLabel, type: 'subcategory' });
        }
      }
    }

    var parts = [];
    if (created.length) parts.push(created.length + ' created');
    if (skipped.length) parts.push(skipped.length + ' skipped');
    if (errors.length)  parts.push(errors.length + ' failed');
    data.stepSummary = parts.join(', ') || 'Nothing to create';

    data.categoryValue = categoryValue;
    data.created = created;
    data.skipped = skipped;
    data.errors = errors;
    return;
  }

  // ── Action: create_close_codes ──
  if (input.action === 'create_close_codes') {
    var categoryValue = input.categoryValue || '';
    if (!categoryValue) { data.errors = ['No category value.']; return; }

    var prefix = derivePrefix(categoryValue);
    var codes = parseLabels(input.closeCodes || '');
    var created = [];
    var skipped = [];
    var errors = [];

    // Get all subcategories for this category
    var allSubValues = [];
    var existingSubGR = new GlideRecord('sys_choice');
    existingSubGR.addQuery('name', 'incident');
    existingSubGR.addQuery('element', 'subcategory');
    existingSubGR.addQuery('dependent_value', categoryValue);
    existingSubGR.addQuery('inactive', false);
    existingSubGR.query();
    while (existingSubGR.next()) {
      allSubValues.push({
        value: existingSubGR.getValue('value'),
        label: existingSubGR.getDisplayValue('label')
      });
    }

    if (allSubValues.length === 0) {
      data.stepSummary = 'No subcategories found \u2014 close codes need at least one subcategory';
      data.created = [];
      data.skipped = [];
      data.errors = [{ text: 'No subcategories exist for this category. Create subcategories first.', type: 'close_code' }];
      return;
    }

    for (var j = 0; j < codes.length; j++) {
      var codeLabel = codes[j];
      var codeValue = prefix + ' - ' + codeLabel.toLowerCase();

      for (var k = 0; k < allSubValues.length; k++) {
        var sub = allSubValues[k];

        if (choiceExists('close_code', codeValue, sub.value)) {
          skipped.push({ text: codeLabel, detail: 'for ' + sub.label, type: 'close_code' });
        } else {
          var codeGR = new GlideRecord('sys_choice');
          codeGR.initialize();
          codeGR.setValue('name', 'incident');
          codeGR.setValue('element', 'close_code');
          codeGR.setValue('value', codeValue);
          codeGR.setValue('label', codeLabel);
          codeGR.setValue('dependent_value', sub.value);
          codeGR.setValue('inactive', false);
          if (codeGR.insert()) {
            created.push({ text: codeLabel, detail: 'for ' + sub.label, type: 'close_code' });
          } else {
            errors.push({ text: 'Failed: ' + codeLabel + ' for ' + sub.label, type: 'close_code' });
          }
        }
      }
    }

    var parts = [];
    if (created.length) parts.push(created.length + ' created');
    if (skipped.length) parts.push(skipped.length + ' skipped');
    if (errors.length)  parts.push(errors.length + ' failed');
    data.stepSummary = parts.join(', ') + ' (' + codes.length + ' codes \u00d7 ' + allSubValues.length + ' subcategories)';

    data.categoryValue = categoryValue;
    data.created = created;
    data.skipped = skipped;
    data.errors = errors;
    return;
  }
})();
