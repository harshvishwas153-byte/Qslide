(function () {
    const BAN_STORAGE_KEY = 'qslideLearnerBannedUntil';
    const STYLE_ID = 'qslide-proctoring-styles';
    const DEFAULT_IGNORE_SELECTORS = [
        '[data-proctor-ignore]',
        '.screen-change-alert',
        '.screen-block-overlay',
        '.timer-box',
        '.result-overlay',
        '.report-overlay',
        '.name-overlay'
    ];

    function injectStyles() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
            .screen-change-alert {
                position: fixed;
                top: 76px;
                right: 18px;
                z-index: 900;
                max-width: min(360px, calc(100vw - 28px));
                border-radius: 12px;
                padding: 12px 14px;
                color: #7f1d1d;
                background: #fef2f2;
                border: 1px solid #fecaca;
                box-shadow: 0 14px 36px rgba(127, 29, 29, 0.16);
                font-family: 'DM Sans', system-ui, sans-serif;
                font-size: 13px;
                font-weight: 700;
                line-height: 1.35;
                display: none;
            }
            .screen-change-alert.show { display: block; }
            .screen-change-alert.high-risk {
                color: #fff;
                background: #dc2626;
                border-color: #991b1b;
            }
            .screen-block-overlay {
                position: fixed;
                inset: 0;
                z-index: 1200;
                background: rgba(15, 23, 42, 0.72);
                backdrop-filter: blur(6px);
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .screen-block-card {
                width: min(100%, 460px);
                border-radius: 18px;
                border: 1px solid #fecaca;
                background: #fff;
                padding: 28px 26px;
                text-align: center;
                box-shadow: 0 28px 80px rgba(15, 23, 42, 0.28);
                font-family: 'DM Sans', system-ui, sans-serif;
            }
            .screen-block-title {
                font-family: 'Syne', system-ui, sans-serif;
                font-size: 22px;
                font-weight: 800;
                color: #991b1b;
                margin-bottom: 8px;
            }
            .screen-block-text {
                color: #57534e;
                font-size: 15px;
                line-height: 1.55;
            }
        `;
        document.head.appendChild(style);
    }

    function getAlertElement() {
        injectStyles();
        let alert = document.querySelector('.screen-change-alert');
        if (!alert) {
            alert = document.createElement('div');
            alert.className = 'screen-change-alert';
            alert.setAttribute('role', 'alert');
            document.body.appendChild(alert);
        }
        return alert;
    }

    function showAlert(message, highRisk) {
        const alert = getAlertElement();
        alert.textContent = message;
        alert.classList.toggle('high-risk', Boolean(highRisk));
        alert.classList.add('show');
    }

    function formatBanTime(untilMs) {
        const date = new Date(untilMs);
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function showLearnerBlock(message) {
        injectStyles();
        let overlay = document.querySelector('.screen-block-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'screen-block-overlay';
            overlay.innerHTML = `
                <div class="screen-block-card">
                    <div class="screen-block-title">Screen Change Detected</div>
                    <div class="screen-block-text"></div>
                </div>
            `;
            document.body.appendChild(overlay);
        }
        overlay.querySelector('.screen-block-text').textContent = message;
    }

    function setLearnerBanUntil(untilMs) {
        try {
            localStorage.setItem(BAN_STORAGE_KEY, String(untilMs));
        } catch (_) {}
    }

    function getLearnerBanUntil() {
        try {
            const untilMs = Number(localStorage.getItem(BAN_STORAGE_KEY));
            if (untilMs && untilMs > Date.now()) return untilMs;
            localStorage.removeItem(BAN_STORAGE_KEY);
        } catch (_) {}
        return null;
    }

    function selectorList(config) {
        return DEFAULT_IGNORE_SELECTORS.concat(config.ignoreSelectors || []);
    }

    function matchesAny(element, selectors) {
        if (!element || !element.matches) return false;
        return selectors.some(selector => {
            try {
                return element.matches(selector) || Boolean(element.closest(selector));
            } catch (_) {
                return false;
            }
        });
    }

    function normalizeClone(clonedDocument) {
        clonedDocument.querySelectorAll('.option').forEach(el => {
            el.classList.remove('correct', 'wrong', 'selected');
            el.removeAttribute('style');
        });
        clonedDocument.querySelectorAll('input').forEach(input => {
            input.checked = false;
            input.disabled = false;
        });
        clonedDocument.querySelectorAll('.feedback').forEach(el => {
            el.className = 'feedback';
            el.textContent = '';
            el.style.display = 'none';
        });
        clonedDocument.querySelectorAll('.progress-bar').forEach(el => {
            el.style.width = '0%';
        });
        clonedDocument.querySelectorAll('.nav-card').forEach(el => {
            el.classList.remove('answered', 'correct-nav', 'wrong-nav');
        });
        clonedDocument.querySelectorAll('[id^="stat-"]').forEach(el => {
            el.textContent = 'Not answered';
            el.removeAttribute('style');
        });
    }

    async function captureFingerprint(config) {
        if (!window.html2canvas) return null;
        const target = document.querySelector(config.targetSelector) || document.body;
        const selectors = selectorList(config);
        const canvas = await window.html2canvas(target, {
            backgroundColor: '#ffffff',
            logging: false,
            useCORS: true,
            scale: config.scale,
            ignoreElements: element => matchesAny(element, selectors),
            onclone: normalizeClone
        });
        if (!canvas.width || !canvas.height) return null;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        return {
            width: canvas.width,
            height: canvas.height,
            data: image.data
        };
    }

    function diffRatio(previous, current, pixelThreshold) {
        if (!previous || !current) return 0;
        if (previous.width !== current.width || previous.height !== current.height) return 1;

        const pixelCount = previous.width * previous.height;
        const pixelStep = Math.max(1, Math.floor(pixelCount / 40000));
        const byteStep = pixelStep * 4;
        let changed = 0;
        let sampled = 0;

        for (let i = 0; i < previous.data.length; i += byteStep) {
            const diff =
                Math.abs(previous.data[i] - current.data[i]) +
                Math.abs(previous.data[i + 1] - current.data[i + 1]) +
                Math.abs(previous.data[i + 2] - current.data[i + 2]);
            if (diff > pixelThreshold) changed++;
            sampled++;
        }

        return sampled ? changed / sampled : 0;
    }

    function ProctoringSession(config) {
        this.config = Object.assign({
            mode: 'student',
            targetSelector: 'body',
            intervalMs: 5000,
            scale: 0.14,
            diffRatioThreshold: 0.12,
            pixelThreshold: 110,
            ignoreSelectors: [],
            cooldownMs: null,     // min time between two detections (default: intervalMs)
            focusDelayMs: null,   // how long the window may lose focus before it counts (default: intervalMs)
            maxDetections: 0,     // when > 0, reaching this count reports limitReached
            quietAfterDetectionMs: 0, // ignore 'page-change' this long after any other detection
            describeReason: null  // optional (reason) => text appended to the on-screen alert
        }, config || {});
        if (this.config.cooldownMs == null) this.config.cooldownMs = this.config.intervalMs;
        if (this.config.focusDelayMs == null) this.config.focusDelayMs = this.config.intervalMs;
        this.awayCounted = false;
        this.quietUntil = 0;
        this.pausedUntil = 0;
        this.count = 0;
        this.running = false;
        this.previous = null;
        this.captureInFlight = false;
        this.lastDetectionAt = 0;
        this.intervalId = null;
        this.focusTimer = null;
        this.handleVisibilityLoss = this.handleVisibilityLoss.bind(this);
        this.handleFocusReturn = this.handleFocusReturn.bind(this);
    }

    ProctoringSession.prototype.start = function () {
        if (this.running) return;
        this.running = true;
        injectStyles();
        this.refreshBaseline();
        this.intervalId = setInterval(() => this.checkScreenshot(), this.config.intervalMs);
        document.addEventListener('visibilitychange', this.handleVisibilityLoss);
        window.addEventListener('blur', this.handleVisibilityLoss);
        window.addEventListener('focus', this.handleFocusReturn);
    };

    ProctoringSession.prototype.stop = function () {
        this.running = false;
        clearInterval(this.intervalId);
        clearTimeout(this.focusTimer);
        document.removeEventListener('visibilitychange', this.handleVisibilityLoss);
        window.removeEventListener('blur', this.handleVisibilityLoss);
        window.removeEventListener('focus', this.handleFocusReturn);
    };

    ProctoringSession.prototype.getCount = function () {
        return this.count;
    };

    ProctoringSession.prototype.refreshBaseline = async function () {
        if (!this.running || this.captureInFlight || document.hidden) return;
        this.captureInFlight = true;
        try {
            this.previous = await captureFingerprint(this.config);
        } catch (_) {
            this.previous = null;
        } finally {
            this.captureInFlight = false;
        }
    };

    ProctoringSession.prototype.checkScreenshot = async function () {
        if (!this.running || this.captureInFlight || document.hidden) return;
        this.captureInFlight = true;
        try {
            const current = await captureFingerprint(this.config);
            const ratio = diffRatio(this.previous, current, this.config.pixelThreshold);
            this.previous = current || this.previous;
            if (ratio > this.config.diffRatioThreshold) {
                this.recordDetection('page-change', ratio);
            }
        } catch (_) {
            this.previous = null;
        } finally {
            this.captureInFlight = false;
        }
    };

    ProctoringSession.prototype.handleVisibilityLoss = function () {
        if (!this.running) return;
        clearTimeout(this.focusTimer);
        if (this.awayCounted) return;
        this.focusTimer = setTimeout(() => {
            if (this.running && !this.awayCounted && (document.hidden || !document.hasFocus())) {
                this.awayCounted = true;   // one count per time the student leaves, not per event
                // tab hidden = switched tab / minimised / other desktop; otherwise another window took focus
                this.recordDetection(document.hidden ? 'tab-hidden' : 'window-blur', 1);
            }
        }, this.config.focusDelayMs);
    };

    ProctoringSession.prototype.handleFocusReturn = function () {
        if (!this.running) return;
        clearTimeout(this.focusTimer);
        this.awayCounted = false;
        setTimeout(() => this.refreshBaseline(), 300);
    };

    // Ignore page-appearance changes for a while (e.g. right after entering/leaving full screen).
    ProctoringSession.prototype.quiet = function (ms) {
        this.quietUntil = Date.now() + ms;
        setTimeout(() => this.refreshBaseline(), Math.min(ms, 800));
    };

    // Ignore every detection for a while (e.g. while a browser dialog such as the screen-share picker is open).
    ProctoringSession.prototype.pause = function (ms) {
        this.pausedUntil = ms > 0 ? Date.now() + ms : 0;
        clearTimeout(this.focusTimer);
        if (ms <= 0) { this.awayCounted = false; this.refreshBaseline(); }
    };

    ProctoringSession.prototype.recordDetection = function (reason, ratio) {
        const now = Date.now();
        if (now < this.pausedUntil) return;
        if (reason === 'page-change') {
            if (now < this.quietUntil || now - this.lastDetectionAt < this.config.quietAfterDetectionMs) {
                this.refreshBaseline();
                return;
            }
        }
        if (now - this.lastDetectionAt < this.config.cooldownMs) return;
        this.lastDetectionAt = now;
        this.count++;

        const highRisk = this.count > 2;
        if (this.config.mode === 'learner') {
            showAlert('Screen change detected. Your quiz has ended.', true);
            this.stop();
            if (typeof this.config.onViolation === 'function') {
                this.config.onViolation({ count: this.count, reason, ratio });
            }
            return;
        }

        const limit = this.config.maxDetections;
        const limitReached = limit > 0 && this.count >= limit;
        const why = typeof this.config.describeReason === 'function' ? this.config.describeReason(reason) : '';
        const message = (limitReached
            ? `Screen changed ${this.count} times. Your test is being submitted.`
            : highRisk
                ? `Screen change detection: red tag active (${this.count} detections).`
                : `Screen change detected (${this.count}/${limit > 0 ? limit : 3}).`) + (why ? ` ${why}` : '');
        showAlert(message, highRisk || limitReached);
        if (limitReached) this.stop();
        if (typeof this.config.onDetection === 'function') {
            this.config.onDetection({ count: this.count, highRisk, limitReached, reason, ratio });
        }
    };

    window.QslideProctoring = {
        create: config => new ProctoringSession(config),
        setLearnerBanUntil,
        getLearnerBanUntil,
        formatBanTime,
        showLearnerBlock
    };
})();
