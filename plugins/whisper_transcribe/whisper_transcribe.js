(function () {
  'use strict';

  // Try both common plugin IDs:
  // - folder name ("whisper_transcribe")
  // - YAML name ("WhisperTranscribe")
  const PLUGIN_IDS = ['whisper_transcribe', 'WhisperTranscribe'];
  const MENU_ITEM_ID = 'whisper-transcribe-menu-item';
  // The three‑dot "operations menu" (ID: operation-menu) that contains actions like rescan, generate, etc.
  const OPERATIONS_TOGGLE_ID = 'operation-menu';

  // ============================================================
  // Toast Notification System
  // ============================================================

  function showToast(message, type = 'success') {
    // Method 1: Stash's PluginApi (newer versions)
    if (window.PluginApi?.libraries?.Toast) {
      try {
        if (type === 'success') {
          window.PluginApi.libraries.Toast.success(message);
        } else if (type === 'error') {
          window.PluginApi.libraries.Toast.error(message);
        } else if (type === 'warning') {
          window.PluginApi.libraries.Toast.warning(message);
        } else {
          window.PluginApi.libraries.Toast.info(message);
        }
        return;
      } catch (e) {
        console.warn('[WhisperTranscribe] PluginApi Toast failed:', e);
      }
    }

    // Method 2: Stash's global stash object (some versions)
    if (window.stash?.Toast) {
      try {
        const toastFn = window.stash.Toast[type] || window.stash.Toast.success;
        if (typeof toastFn === 'function') {
          toastFn(message);
          return;
        }
      } catch (e) {
        console.warn('[WhisperTranscribe] stash.Toast failed:', e);
      }
    }

    // Method 3: Look for react-toastify container and dispatch event
    const toastContainer = document.querySelector('.Toastify');
    if (toastContainer && window.dispatchEvent) {
      try {
        const event = new CustomEvent('stash:toast', {
          detail: { message, type },
          bubbles: true,
        });
        document.dispatchEvent(event);
      } catch (e) {
        console.warn('[WhisperTranscribe] Custom event dispatch failed:', e);
      }
    }

    // Method 4: Fallback - create a simple toast element
    createFallbackToast(message, type);
  }

  function createFallbackToast(message, type) {
    // Remove any existing whisper toast
    const existing = document.getElementById('whisper-transcribe-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'whisper-transcribe-toast';
    toast.textContent = message;

    const bgColor =
      type === 'error'
        ? '#dc3545'
        : type === 'warning'
          ? '#ffc107'
          : '#28a745';
    const textColor = type === 'warning' ? '#212529' : '#ffffff';

    toast.style.cssText = `
      position: fixed;
      bottom: 20px;
      right: 20px;
      padding: 12px 20px;
      border-radius: 4px;
      color: ${textColor};
      font-size: 14px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      z-index: 99999;
      background-color: ${bgColor};
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
      opacity: 0;
      transform: translateY(10px);
      transition: opacity 0.3s ease, transform 0.3s ease;
      max-width: 350px;
      word-wrap: break-word;
    `;

    document.body.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => {
      toast.style.opacity = '1';
      toast.style.transform = 'translateY(0)';
    });

    // Auto-dismiss after 5 seconds
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      setTimeout(() => toast.remove(), 300);
    }, 5000);
  }

  // ============================================================
  // Job Polling System
  // ============================================================

  async function pollJobStatus(jobId, graphqlURL, description) {
    const query = `
      query FindJob($id: ID!) {
        findJob(input: { id: $id }) {
          id
          status
          subTasks
          description
          progress
          error
        }
      }
    `;

    let pollCount = 0;
    const maxPolls = 1800; // Max ~1 hour at 2-second intervals
    const pollInterval = 2000; // 2 seconds

    const poll = async () => {
      pollCount++;

      if (pollCount > maxPolls) {
        console.warn('[WhisperTranscribe] Polling timed out after max attempts');
        showToast('Transcription status unknown (polling timed out)', 'warning');
        return;
      }

      try {
        const res = await fetch(graphqlURL, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query, variables: { id: jobId } }),
        });

        if (!res.ok) {
          console.warn('[WhisperTranscribe] Poll request failed:', res.status);
          setTimeout(poll, pollInterval);
          return;
        }

        const json = await res.json();

        if (json.errors) {
          console.warn('[WhisperTranscribe] Poll GraphQL errors:', json.errors);
          setTimeout(poll, pollInterval);
          return;
        }

        const job = json.data?.findJob;

        // Job no longer exists - likely completed and removed from queue
        if (!job) {
          console.debug('[WhisperTranscribe] Job no longer in queue, assuming completed');
          showToast(`Transcription completed: ${description || jobId}`, 'success');
          return;
        }

        const status = (job.status || '').toUpperCase();

        if (status === 'FINISHED') {
          console.debug('[WhisperTranscribe] Job finished successfully');
          showToast(`Transcription completed: ${description || jobId}`, 'success');
          return;
        }

        if (status === 'CANCELLED') {
          console.debug('[WhisperTranscribe] Job was cancelled');
          showToast(`Transcription cancelled: ${description || jobId}`, 'warning');
          return;
        }

        if (status === 'FAILED') {
          const errorMsg = job.error || 'Unknown error';
          console.error('[WhisperTranscribe] Job failed:', errorMsg);
          showToast(`Transcription failed: ${errorMsg}`, 'error');
          return;
        }

        // Still running (READY, RUNNING, etc.) - continue polling
        console.debug(`[WhisperTranscribe] Job status: ${status}, progress: ${job.progress || 0}%`);
        setTimeout(poll, pollInterval);
      } catch (e) {
        console.error('[WhisperTranscribe] Poll error:', e);
        // Continue polling on network errors
        setTimeout(poll, pollInterval);
      }
    };

    // Start polling
    poll();
  }

  // ============================================================
  // Core Functions
  // ============================================================

  function getSceneIdFromURL() {
    try {
      // Try pathname first: /scenes/123
      const pathMatch = window.location.pathname.match(/\/scenes\/(\d+)/);
      if (pathMatch) return parseInt(pathMatch[1], 10);

      // Fallback to hash routes: #/scenes/123
      const hashMatch = window.location.hash.match(/\/scenes\/(\d+)/);
      if (hashMatch) return parseInt(hashMatch[1], 10);
    } catch (e) {
      console.warn('[WhisperTranscribe] Failed to parse scene id from URL:', e);
    }
    return undefined;
  }

  async function resolvePluginId(graphqlURL) {
    const query = `query { plugins { id name } }`;
    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const json = await res.json();
      if (json.errors || !json.data || !json.data.plugins) return null;

      const plugins = json.data.plugins;

      // Prefer exact id matches first
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.id)) return p.id;
      }
      // Then match by name
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.name)) return p.id;
      }
      // Heuristic fallback: anything containing "whisper"
      for (const p of plugins) {
        const n = (p.name || '').toLowerCase();
        const i = (p.id || '').toLowerCase();
        if (n.includes('whisper') || i.includes('whisper')) return p.id;
      }
      return null;
    } catch (e) {
      console.error('[WhisperTranscribe] Failed to resolve plugin id:', e);
      return null;
    }
  }

  function basename(path) {
    if (typeof path !== 'string') return undefined;
    const trimmed = path.trim();
    if (!trimmed) return undefined;
    const parts = trimmed.split(/[\\/]/);
    return parts[parts.length - 1] || undefined;
  }

  async function buildJobDescription(graphqlURL, sceneId) {
    const query = `
      query WhisperTranscribeScene($id: ID!) {
        findScene(id: $id) {
          title
          files {
            path
          }
        }
      }
    `;

    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, variables: { id: sceneId } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);

      const json = await res.json();
      if (json.errors || !json.data || !json.data.findScene) return `scene ${sceneId}`;

      const scene = json.data.findScene;
      const filePath = scene.files?.[0]?.path;
      const fileLabel = basename(filePath);
      if (fileLabel) return fileLabel;

      const title = (scene.title || '').trim();
      if (title) return title;

      return `scene ${sceneId}`;
    } catch (e) {
      console.warn('[WhisperTranscribe] Failed to build job description:', e);
      return `scene ${sceneId}`;
    }
  }

  async function runTranscribe(sceneId) {
    const mutation = `
      mutation RunPluginTask($plugin_id: ID!, $args_map: Map!, $description: String) {
        runPluginTask(plugin_id: $plugin_id, args_map: $args_map, description: $description)
      }
    `;
    const args_map = { mode: 'transcribe_scene_task', scene_id: sceneId };
    const base = document.querySelector('base')?.getAttribute('href') || '/';
    const graphqlURL = new URL('graphql', new URL(base, window.location.href)).toString();

    // Resolve plugin id; if not found, abort to avoid server-side panic on unknown id.
    const resolvedId = await resolvePluginId(graphqlURL);
    if (!resolvedId) {
      console.error('[WhisperTranscribe] Could not resolve plugin id. Aborting to avoid server error.');
      showToast('Whisper Transcribe plugin not found. Try reloading plugins.', 'error');
      return;
    }

    const sceneLabel = await buildJobDescription(graphqlURL, sceneId);
    const description = `whisper_transcribe: ${sceneLabel}`;

    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: mutation, variables: { plugin_id: resolvedId, args_map, description } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const json = await res.json();
      if (json.errors) {
        console.error('[WhisperTranscribe] GraphQL errors:', json.errors);
        showToast('Failed to start transcription. See console for details.', 'error');
        return;
      }

      const jobId = json.data?.runPluginTask;
      console.debug('[WhisperTranscribe] Transcription queued as job:', jobId);
      showToast(`Transcription started: ${sceneLabel}`, 'info');

      // Start polling for job completion
      if (jobId) {
        pollJobStatus(jobId, graphqlURL, sceneLabel);
      }
    } catch (e) {
      console.error('[WhisperTranscribe] Request failed:', e);
      showToast('Failed to start transcription. See console for details.', 'error');
    }
  }

  // ============================================================
  // Menu Item Integration
  // ============================================================

  function closeDropdown(menuEl) {
    const dropdown = menuEl?.closest('.dropdown');
    menuEl?.classList.remove('show');
    dropdown?.classList.remove('show');
  }

  function createMenuItem(menuEl) {
    if (!menuEl) return;
    const existing = document.getElementById(MENU_ITEM_ID);
    if (existing) {
      // If it's already in the correct menu, nothing to do.
      if (menuEl.contains(existing)) return;
      existing.remove();
    }

    const item = document.createElement('button');
    item.id = MENU_ITEM_ID;
    item.type = 'button';
    item.className = 'dropdown-item bg-secondary text-white';
    item.textContent = 'Transcribe scene (Whisper)';
    item.addEventListener('click', function (ev) {
      ev.preventDefault();
      const sceneId = getSceneIdFromURL();
      if (!sceneId) {
        showToast('Could not determine scene ID from URL.', 'error');
        return;
      }
      runTranscribe(sceneId);
      closeDropdown(menuEl);
    });

    // Try to position after "Generate default thumbnail"
    const items = Array.from(menuEl.querySelectorAll('.dropdown-item'));
    const defaultThumbItem = items.find((el) => {
      const text = (el.textContent || '').trim().toLowerCase();
      return text.includes('generate default thumbnail');
    });
    if (defaultThumbItem?.parentElement === menuEl) {
      defaultThumbItem.insertAdjacentElement('afterend', item);
    } else {
      // Fall back: place before delete to keep destructive actions at the end.
      const deleteItem = items.find((el) => {
        const text = (el.textContent || '').trim().toLowerCase();
        return text.includes('delete');
      });
      if (deleteItem?.parentElement === menuEl) {
        menuEl.insertBefore(item, deleteItem);
      } else {
        menuEl.appendChild(item);
      }
    }
  }

  function findOperationsMenu() {
    const toggle = document.getElementById(OPERATIONS_TOGGLE_ID);
    if (!toggle) return null;
    const dropdown = toggle.closest('.dropdown');
    if (!dropdown) return null;
    const menuEl = dropdown.querySelector('.dropdown-menu');
    if (!menuEl) return null;
    return menuEl;
  }

  function mountIfPossible() {
    if (!getSceneIdFromURL()) return false;
    const menuEl = findOperationsMenu();
    if (!menuEl) return false;
    createMenuItem(menuEl);
    return true;
  }

  // ============================================================
  // Initialization
  // ============================================================

  // Register as a Stash UI task if possible; fallback to menu item.
  if (typeof window.registerTask === 'function') {
    window.registerTask({
      name: 'Transcribe scene (Whisper)',
      description: 'Transcribe the current scene using Whisper',
      icon: 'fa-microphone',
      handler: async () => {
        const sceneId = getSceneIdFromURL();
        if (!sceneId) {
          showToast('Could not determine scene ID from URL.', 'error');
          return;
        }
        await runTranscribe(sceneId);
      },
    });
    console.debug('[WhisperTranscribe] Task registered via registerTask');
  } else {
    // Fallback to original menu item approach.
    mountIfPossible();

    // Observe DOM changes for SPA navigation and render timing
    const observer = new MutationObserver((mutationsList) => {
      for (const mutation of mutationsList) {
        for (const addedNode of mutation.addedNodes) {
          if (addedNode.nodeType !== Node.ELEMENT_NODE) continue;

          // If the operations menu or its toggle appears, attempt mount.
          if (
            addedNode.id === OPERATIONS_TOGGLE_ID ||
            addedNode.querySelector?.(`#${OPERATIONS_TOGGLE_ID}`) ||
            addedNode.classList?.contains('dropdown-menu')
          ) {
            mountIfPossible();
            return;
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  console.debug('[WhisperTranscribe] UI script initialized');
})();