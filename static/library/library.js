(() => {
  const audio = document.querySelector("#library-audio");
  if (!audio) return;

  const title = document.querySelector("#player-title");
  const album = document.querySelector("#player-album");
  const seek = document.querySelector("#player-seek");
  const currentTime = document.querySelector("#player-current");
  const duration = document.querySelector("#player-duration");
  const volume = document.querySelector("#player-volume");
  const queueCount = document.querySelector("#queue-count");
  const queueList = document.querySelector("#queue-list");
  let queue = [];
  let queueIndex = -1;
  let audioContext;
  let filters = [];

  const formatTime = (seconds) => {
    if (!Number.isFinite(seconds)) return "0:00";
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  };

  const ensureAudioGraph = async () => {
    if (!audioContext) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return;
      audioContext = new AudioContextClass();
      const source = audioContext.createMediaElementSource(audio);
      let previous = source;
      for (const frequency of [60, 250, 1000, 4000, 12000]) {
        const filter = audioContext.createBiquadFilter();
        filter.type = "peaking";
        filter.frequency.value = frequency;
        filter.Q.value = 0.8;
        previous.connect(filter);
        previous = filter;
        filters.push(filter);
      }
      previous.connect(audioContext.destination);
      document.querySelectorAll("[data-eq]").forEach((control) => {
        control.addEventListener("input", () => {
          const index = [60, 250, 1000, 4000, 12000].indexOf(Number(control.dataset.eq));
          if (filters[index]) filters[index].gain.value = Number(control.value);
        });
      });
    }
    if (audioContext.state === "suspended") await audioContext.resume();
  };

  const loadTrack = async (track, index = 0, nextQueue = [track], autoplay = true) => {
    queue = nextQueue;
    queueIndex = index;
    const requestedUrl = new URL(track.url, window.location.href).href;
    audio.src = requestedUrl;
    title.textContent = track.title || "Unknown track";
    album.textContent = track.album || "Library";
    audio.load();
    if (!autoplay) return;
    try {
      await ensureAudioGraph();
      await audio.play();
    } catch (error) {
      if (audio.src === requestedUrl && error.name !== "AbortError") {
        title.textContent = "Playback unavailable";
      }
      console.error(error);
    }
  };

  const renderQueue = () => {
    queueCount.textContent = `QUEUE ${queue.length}`;
    queueList.replaceChildren();
    queue.forEach((track, index) => {
      const item = document.createElement("li");
      item.className = index === queueIndex ? "queue-item queue-item-current" : "queue-item";
      const info = document.createElement("span");
      info.className = "queue-item-info";
      const trackTitle = document.createElement("strong");
      trackTitle.textContent = track.title || "Unknown track";
      const trackAlbum = document.createElement("small");
      trackAlbum.textContent = [track.artist, track.album].filter(Boolean).join(" · ") || "Library";
      info.append(trackTitle, trackAlbum);

      const playButton = document.createElement("button");
      playButton.type = "button";
      playButton.className = "queue-item-action";
      playButton.dataset.queuePlay = String(index);
      playButton.setAttribute("aria-label", `Play ${track.title || "track"}`);
      playButton.textContent = "▶";

      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "queue-item-action queue-remove";
      removeButton.dataset.queueRemove = String(index);
      removeButton.setAttribute("aria-label", `Remove ${track.title || "track"} from queue`);
      removeButton.textContent = "×";
      removeButton.disabled = index === queueIndex;

      item.append(info, playButton, removeButton);
      queueList.append(item);
    });
  };

  const enqueueTracks = (tracks) => {
    const playableTracks = tracks.filter((track) => {
      if (!track?.url || !track.title) return false;
      return new URL(track.url, window.location.href).origin === window.location.origin;
    });
    queue.push(...playableTracks);
    renderQueue();
  };

  const playNext = async () => {
    const nextIndex = queueIndex < 0 ? 0 : queueIndex + 1;
    if (queue[nextIndex]) await loadTrack(queue[nextIndex], nextIndex, queue);
  };

  const playPrevious = async () => {
    if (queueIndex > 0) {
      const previousIndex = queueIndex - 1;
      await loadTrack(queue[previousIndex], previousIndex, queue);
    } else if (queueIndex < 0 && queue.length) {
      await loadTrack(queue[0], 0, queue);
    } else if (audio.src) {
      audio.currentTime = 0;
      await audio.play().catch(console.error);
    }
  };

  document.addEventListener("click", async (event) => {
    const queueTrackButton = event.target.closest("[data-queue-track]");
    if (queueTrackButton) {
      enqueueTracks([{
        url: queueTrackButton.dataset.queueTrack,
        title: queueTrackButton.dataset.trackTitle,
        album: queueTrackButton.dataset.trackAlbum,
        artist: queueTrackButton.dataset.trackArtist,
      }]);
      return;
    }

    const enqueuePlaylistButton = event.target.closest("[data-enqueue-playlist]");
    if (enqueuePlaylistButton) {
      const source = document.getElementById(enqueuePlaylistButton.dataset.enqueuePlaylist);
      if (source) enqueueTracks(JSON.parse(source.textContent));
      return;
    }

    const queuedPlayButton = event.target.closest("[data-queue-play]");
    if (queuedPlayButton) {
      const index = Number(queuedPlayButton.dataset.queuePlay);
      if (queue[index]) await loadTrack(queue[index], index, queue);
      return;
    }

    const queuedRemoveButton = event.target.closest("[data-queue-remove]");
    if (queuedRemoveButton) {
      const index = Number(queuedRemoveButton.dataset.queueRemove);
      if (index !== queueIndex) {
        queue.splice(index, 1);
        if (index < queueIndex) queueIndex -= 1;
        renderQueue();
      }
      return;
    }

    if (event.target.closest("[data-clear-queue]")) {
      queue = [];
      queueIndex = -1;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      title.textContent = "Choose a track";
      album.textContent = "Your library";
      currentTime.textContent = "0:00";
      duration.textContent = "0:00";
      seek.value = "0";
      renderQueue();
      return;
    }

    const playTrackButton = event.target.closest("[data-play-track]");
    if (playTrackButton) {
      await loadTrack({
        url: playTrackButton.dataset.playTrack,
        title: playTrackButton.dataset.trackTitle,
        album: playTrackButton.dataset.trackAlbum,
      });
      return;
    }

    const playlistButton = event.target.closest("[data-playlist]");
    if (playlistButton) {
      const source = document.getElementById(playlistButton.dataset.playlist);
      if (!source) return;
      const tracks = JSON.parse(source.textContent);
      if (tracks.length) await loadTrack(tracks[0], 0, tracks);
      return;
    }

    const actionButton = event.target.closest("[data-player-action]");
    if (actionButton) {
      switch (actionButton.dataset.playerAction) {
        case "play":
          if (audio.src) {
            await ensureAudioGraph();
            audio.play().catch(console.error);
          } else if (queue.length) {
            await playNext();
          }
          break;
        case "pause":
          audio.pause();
          break;
        case "stop":
          audio.pause();
          if (audio.src) audio.currentTime = 0;
          break;
        case "previous":
          await playPrevious();
          break;
        case "next":
          await playNext();
          break;
        case "back-10":
          if (audio.src) audio.currentTime = Math.max(0, audio.currentTime - 10);
          break;
        case "forward-10":
          if (audio.src && Number.isFinite(audio.duration)) {
            audio.currentTime = Math.min(audio.duration, audio.currentTime + 10);
          }
          break;
      }
      return;
    }

    const link = event.target.closest("a[href]");
    if (!link || !link.closest(".app-frame") || link.target || link.hasAttribute("download") || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || url.hash || url.pathname.startsWith("/admin/")) return;
    event.preventDefault();
    navigate(url.href);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.closest(".app-frame") || form.action.includes("/accounts/logout/")) return;
    event.preventDefault();
    const method = (form.method || "get").toUpperCase();
    const url = new URL(form.action || window.location.href, window.location.href);
    if (method === "GET") {
      for (const [key, value] of new FormData(form)) url.searchParams.set(key, value);
      navigate(url.href);
      return;
    }
    navigate(url.href, {
      method,
      body: new FormData(form),
      headers: { "X-CSRFToken": form.querySelector("[name=csrfmiddlewaretoken]")?.value || "" },
    });
  });

  async function navigate(url, options = {}, addHistory = true) {
    try {
      const response = await fetch(url, {
        ...options,
        credentials: "same-origin",
        headers: { ...(options.headers || {}), "X-Requested-With": "XMLHttpRequest" },
      });
      if (response.redirected && new URL(response.url).pathname.startsWith("/accounts/login/")) {
        window.location.assign(response.url);
        return;
      }
      const html = await response.text();
      const parsed = new DOMParser().parseFromString(html, "text/html");
      const newFrame = parsed.querySelector(".app-frame");
      const currentFrame = document.querySelector(".app-frame");
      if (!response.ok || !newFrame || !currentFrame) {
        window.location.assign(response.url || url);
        return;
      }
      currentFrame.replaceWith(newFrame);
      document.title = parsed.title;
      if (addHistory) history.pushState({}, "", response.url || url);
      window.scrollTo({ top: 0, behavior: "instant" });
      scheduleScanPolling();
      window.dispatchEvent(new Event("library:page-updated"));
    } catch (error) {
      window.location.assign(url);
    }
  }

  window.addEventListener("popstate", () => navigate(window.location.href, {}, false));
  audio.addEventListener("timeupdate", () => {
    currentTime.textContent = formatTime(audio.currentTime);
    seek.value = audio.duration ? String(Math.round(audio.currentTime / audio.duration * 1000)) : "0";
  });
  audio.addEventListener("loadedmetadata", () => { duration.textContent = formatTime(audio.duration); });
  audio.addEventListener("ended", playNext);
  seek.addEventListener("input", () => {
    if (audio.duration) audio.currentTime = Number(seek.value) / 1000 * audio.duration;
  });
  volume.addEventListener("input", () => { audio.volume = Number(volume.value); });
  audio.volume = Number(volume.value);
  renderQueue();

  let scanPollTimer;
  function scheduleScanPolling() {
    window.clearTimeout(scanPollTimer);
    const activeScan = document.querySelector(".dashboard-heading[data-scan-status='scanning']");
    if (!activeScan) return;
    scanPollTimer = window.setTimeout(async () => {
      await navigate(window.location.href, {}, false);
      scheduleScanPolling();
    }, 2000);
  }

  scheduleScanPolling();
})();