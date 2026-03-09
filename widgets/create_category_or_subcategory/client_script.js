function(spUtil) {
  var c = this;

  c.data.categoryMode = 'existing';
  c.data.selectedCategory = '';
  c.data.newCategoryLabel = '';
  c.data.subcategories = '';
  c.data.closeCodes = '';
  c.data.submitting = false;
  c.data.progress = null;
  c.data.summary = null;
  c.data.hierarchy = c.data.hierarchy || null;

  // ── Helpers ──

  c.getPrefix = function() {
    var catValue = '';
    if (c.data.categoryMode === 'existing' && c.data.selectedCategory) {
      catValue = c.data.selectedCategory;
    } else if (c.data.categoryMode === 'new' && c.data.newCategoryLabel) {
      catValue = c.data.newCategoryLabel.trim().toLowerCase();
    }
    if (!catValue) return '';
    var parts = catValue.split(' - ');
    return parts.length > 1
      ? parts.slice(1).join(' - ').toLowerCase()
      : parts[0].toLowerCase();
  };

  c.parsePreview = function(text) {
    if (!text) return [];
    var prefix = c.getPrefix();
    if (!prefix) return [];
    return text.split(/[\n,]+/)
      .map(function(s) { return s.trim(); })
      .filter(function(s) { return s.length > 0; })
      .map(function(label) {
        return { label: label, value: prefix + ' - ' + label.toLowerCase() };
      });
  };

  // ── Hierarchy ──

  c.loadHierarchy = function() {
    c.data.action = 'load_hierarchy';
    c.data.categoryValue = c.data.selectedCategory;
    c.server.update().then(function(response) {
      c.data.hierarchy = response.data.hierarchy;
    });
  };

  c.onCategoryChange = function() {
    c.resetResults();
    c.data.hierarchy = null;
    if (c.data.selectedCategory) {
      c.loadHierarchy();
    }
  };

  c.onModeChange = function() {
    c.resetResults();
    c.data.hierarchy = null;
  };

  c.resetResults = function() {
    c.data.progress = null;
    c.data.summary = null;
  };

  // ── Detail panel toggle ──

  c.detailOpen = {};
  c.toggleDetail = function(key) {
    c.detailOpen[key] = !c.detailOpen[key];
  };

  // ── Multi-step submit ──

  c.submit = function() {
    if (c.data.categoryMode === 'existing' && !c.data.selectedCategory) {
      spUtil.addErrorMessage('Please select a category.');
      return;
    }
    if (c.data.categoryMode === 'new' && !c.data.newCategoryLabel.trim()) {
      spUtil.addErrorMessage('Please enter a new category name.');
      return;
    }
    if (!c.data.subcategories.trim() && !c.data.closeCodes.trim()) {
      spUtil.addErrorMessage('Please enter at least one subcategory or close code.');
      return;
    }

    // Build step list based on what needs to be done
    var steps = [];
    if (c.data.categoryMode === 'new') {
      steps.push({ label: 'Creating category', action: 'create_category', status: 'pending', summary: '' });
    }
    if (c.data.subcategories.trim()) {
      var subCount = c.parsePreview(c.data.subcategories).length;
      steps.push({ label: 'Creating ' + subCount + ' subcategor' + (subCount === 1 ? 'y' : 'ies'), action: 'create_subcategories', status: 'pending', summary: '' });
    }
    if (c.data.closeCodes.trim()) {
      steps.push({ label: 'Creating close codes', action: 'create_close_codes', status: 'pending', summary: '' });
    }

    c.data.submitting = true;
    c.data.summary = null;
    c.data.progress = { steps: steps, currentStep: 0, done: false };

    // Prepare shared input
    c.data.isNewCategory = (c.data.categoryMode === 'new');
    c.data.categoryValue = (c.data.categoryMode === 'existing') ? c.data.selectedCategory : '';
    c.data.categoryLabel = (c.data.categoryMode === 'new') ? c.data.newCategoryLabel.trim() : '';

    c._allCreated = [];
    c._allSkipped = [];
    c._allErrors = [];

    c.runNextStep();
  };

  c.runNextStep = function() {
    var p = c.data.progress;
    if (p.currentStep >= p.steps.length) {
      // All steps done
      p.done = true;
      c.data.submitting = false;
      c.data.summary = {
        created: c._allCreated,
        skipped: c._allSkipped,
        errors: c._allErrors
      };
      // Reload hierarchy to show new entries
      var catVal = c.data.categoryValue || c.data.selectedCategory;
      if (catVal) {
        c.data.action = 'load_hierarchy';
        c.data.categoryValue = catVal;
        c.server.update().then(function(resp) {
          c.data.hierarchy = resp.data.hierarchy;
        });
      }
      return;
    }

    var step = p.steps[p.currentStep];
    step.status = 'active';
    c.data.action = step.action;

    c.server.update().then(function(response) {
      var d = response.data;
      step.status = 'done';
      step.summary = d.stepSummary || '';

      // Capture categoryValue from server (needed when creating new category)
      if (d.categoryValue) {
        c.data.categoryValue = d.categoryValue;
      }

      if (d.created) c._allCreated = c._allCreated.concat(d.created);
      if (d.skipped) c._allSkipped = c._allSkipped.concat(d.skipped);
      if (d.errors)  c._allErrors  = c._allErrors.concat(d.errors);

      p.currentStep++;
      c.runNextStep();
    }).catch(function() {
      step.status = 'error';
      step.summary = 'Request failed';
      c.data.submitting = false;
      p.done = true;
      c.data.summary = {
        created: c._allCreated,
        skipped: c._allSkipped,
        errors: c._allErrors
      };
      spUtil.addErrorMessage('An error occurred during: ' + step.label);
    });
  };
}
