// app.js - Dual Knob Interface with Web Serial & Physics

// DOM Elements
const knobChassis = document.getElementById('knobChassis');
const controlKnob = document.getElementById('controlKnob'); // Translucent Foreground
const motorKnob = document.getElementById('motorKnob');     // Opaque Background
const ticksSvg = document.getElementById('ticksSvg');
const errorArc = document.getElementById('errorArc');
const angleTooltip = document.getElementById('angleTooltip');

// Readouts
const displayTarget = document.getElementById('displayTarget');
const displayActual = document.getElementById('displayActual');
const displayError = document.getElementById('displayError');
const displayRpm = document.getElementById('displayRpm');
const displayHz = document.getElementById('displayHz');
const errorBar = document.getElementById('errorBar');
const errorBadge = document.getElementById('errorBadge');
const connStatus = document.getElementById('connStatus');
const btnConnect = document.getElementById('btnConnect');
const btnZero = document.getElementById('btnZero');
const btnMotorDir = document.getElementById('btnMotorDir');
const motorDirText = document.getElementById('motorDirText');
const btnEncoderDir = document.getElementById('btnEncoderDir');
const encoderDirText = document.getElementById('encoderDirText');

// State Variables
let targetAngle = 0.0;     // Commanded continuous angle (translucent knob)
let actualAngle = 0.0;     // Physical motor angle from AS5600 (opaque knob)
let motorRpm = 0.0;
let isDragging = false;
let startPointerAngle = 0;
let startTargetAngle = 0;
let lastDragAngle = 0;
let continuousTurns = 0;

// Separate Direction Controls:
let invertMotor = false;   // Normal: motor matches translucent knob
let invertEncoder = true;  // Inverted: flips encoder sign so opaque knob matches physical movement!

// Web Serial API handles
let port = null;
let reader = null;
let writer = null;
let isConnected = false;
let serialSendTimer = null;
let pendingSendAngle = null;

// ============================================================
// 1. GENERATE DIAL TICKS & DEGREE LABELS (SVG)
// ============================================================
function generateDialTicks() {
  const center = 220;
  const radius = 200;
  let svgContent = '';

  for (let deg = 0; deg < 360; deg += 5) {
    const rad = (deg - 90) * (Math.PI / 180);
    const isMajor = deg % 30 === 0;
    const isMedium = deg % 15 === 0 && !isMajor;
    const tickLen = isMajor ? 14 : (isMedium ? 9 : 5);

    const x1 = center + (radius - tickLen) * Math.cos(rad);
    const y1 = center + (radius - tickLen) * Math.sin(rad);
    const x2 = center + radius * Math.cos(rad);
    const y2 = center + radius * Math.sin(rad);

    const strokeColor = isMajor ? '#00f0ff' : (isMedium ? 'rgba(255,255,255,0.4)' : 'rgba(255,255,255,0.15)');
    const strokeWidth = isMajor ? 2.5 : (isMedium ? 1.5 : 1);

    svgContent += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${strokeColor}" stroke-width="${strokeWidth}" stroke-linecap="round"/>`;

    if (isMajor) {
      const textR = radius - 26;
      const tx = center + textR * Math.cos(rad);
      const ty = center + textR * Math.sin(rad) + 4;
      svgContent += `<text x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" fill="rgba(255,255,255,0.6)" font-size="11" font-family="'JetBrains Mono', monospace" text-anchor="middle">${deg}°</text>`;
    }
  }

  ticksSvg.innerHTML = svgContent;
}

// ============================================================
// 2. DRAW DYNAMIC ERROR ARC BETWEEN TARGET & ACTUAL
// ============================================================
function updateErrorArc(target, actual) {
  const diff = target - actual;
  const absDiff = Math.abs(diff);

  if (absDiff < 0.5) {
    errorArc.setAttribute('d', '');
    return;
  }

  const center = 220;
  const r = 168; // Radius between outer and inner knob
  const startDeg = actual - 90;
  const sweepDeg = Math.max(-180, Math.min(180, diff));
  const endDeg = startDeg + sweepDeg;

  const startRad = startDeg * (Math.PI / 180);
  const endRad = endDeg * (Math.PI / 180);

  const x1 = center + r * Math.cos(startRad);
  const y1 = center + r * Math.sin(startRad);
  const x2 = center + r * Math.cos(endRad);
  const y2 = center + r * Math.sin(endRad);

  const largeArcFlag = Math.abs(sweepDeg) > 180 ? 1 : 0;
  const sweepFlag = sweepDeg > 0 ? 1 : 0;

  const d = `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${r} ${r} 0 ${largeArcFlag} ${sweepFlag} ${x2.toFixed(1)} ${y2.toFixed(1)}`;
  errorArc.setAttribute('d', d);

  if (absDiff > 25) {
    errorArc.setAttribute('stroke', 'rgba(239, 68, 68, 0.7)'); // Red on high lag
  } else {
    errorArc.setAttribute('stroke', 'rgba(255, 170, 0, 0.6)'); // Amber
  }
}

// ============================================================
// 3. UI UPDATE / TELEMETRY REFRESH
// ============================================================
function updateVisuals() {
  // Rotate Translucent Knob (User Setpoint)
  controlKnob.style.transform = `rotate(${targetAngle}deg)`;

  // Rotate Opaque Knob (Physical Motor)
  motorKnob.style.transform = `rotate(${actualAngle}deg)`;

  // Readouts
  displayTarget.innerHTML = `${targetAngle.toFixed(1)}<span class="unit">°</span>`;
  displayActual.innerHTML = `${actualAngle.toFixed(1)}<span class="unit">°</span>`;

  const err = targetAngle - actualAngle;
  const absErr = Math.abs(err);
  displayError.innerHTML = `${absErr.toFixed(1)}<span class="unit">°</span>`;

  // Error bar & status
  const barPercent = Math.min(100, (absErr / 45) * 100);
  errorBar.style.width = `${barPercent}%`;

  if (absErr < 2.0) {
    errorBadge.textContent = 'LOCKED';
    errorBadge.className = 'badge green';
    errorBar.style.backgroundColor = 'var(--accent-green)';
  } else if (absErr < 15.0) {
    errorBadge.textContent = 'TRACKING';
    errorBadge.className = 'badge cyan';
    errorBar.style.backgroundColor = 'var(--accent-cyan)';
  } else {
    errorBadge.textContent = 'SLEWING';
    errorBadge.className = 'badge amber';
    errorBar.style.backgroundColor = 'var(--accent-amber)';
  }

  // Speed
  displayRpm.innerHTML = `${Math.abs(motorRpm).toFixed(1)}<span class="unit">RPM</span>`;
  const elecHz = (Math.abs(motorRpm) / 60.0) * 7.0;
  displayHz.textContent = `${elecHz.toFixed(1)} Hz electrical`;

  // Error Arc
  updateErrorArc(targetAngle, actualAngle);
  angleTooltip.textContent = `${targetAngle.toFixed(1)}°`;
}

// ============================================================
// 4. INTERACTIVE DRAG & WHEEL PHYSICS FOR TRANSLUCENT KNOB
// ============================================================
function getPointerAngle(e) {
  const rect = knobChassis.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;

  const dx = clientX - cx;
  const dy = clientY - cy;
  let deg = Math.atan2(dy, dx) * (180 / Math.PI) + 90; // 0 deg is Top
  if (deg < 0) deg += 360;
  return deg;
}

function onPointerDown(e) {
  isDragging = true;
  startPointerAngle = getPointerAngle(e);
  startTargetAngle = targetAngle;
  lastDragAngle = startPointerAngle;

  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  e.preventDefault();
}

function onPointerMove(e) {
  if (!isDragging) return;
  const currentAngle = getPointerAngle(e);
  let delta = currentAngle - lastDragAngle;

  // Handle 360 degree wraparound
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;

  targetAngle += delta;
  lastDragAngle = currentAngle;

  updateVisuals();
  scheduleSerialSend(targetAngle);
}

function onPointerUp() {
  isDragging = false;
  window.removeEventListener('pointermove', onPointerMove);
  window.removeEventListener('pointerup', onPointerUp);
}

// Mouse Wheel for fine degree adjustment
knobChassis.addEventListener('wheel', (e) => {
  e.preventDefault();
  const delta = e.deltaY < 0 ? 2.5 : -2.5;
  targetAngle += delta;
  updateVisuals();
  scheduleSerialSend(targetAngle);
}, { passive: false });

controlKnob.addEventListener('pointerdown', onPointerDown);

// ============================================================
// 5. PRESET BUTTONS
// ============================================================
document.querySelectorAll('.btn-preset').forEach(btn => {
  btn.addEventListener('click', () => {
    const deg = parseFloat(btn.getAttribute('data-deg'));
    animateToAngle(deg);
  });
});

function animateToAngle(targetDeg) {
  const start = targetAngle;
  const diff = targetDeg - start;
  const duration = Math.min(800, Math.max(250, Math.abs(diff) * 2));
  const startTime = performance.now();

  function step(now) {
    const elapsed = now - startTime;
    const progress = Math.min(1, elapsed / duration);
    // Smooth ease-out quad
    const ease = 1 - (1 - progress) * (1 - progress);
    targetAngle = start + diff * ease;
    updateVisuals();
    scheduleSerialSend(targetAngle);

    if (progress < 1) {
      requestAnimationFrame(step);
    }
  }
  requestAnimationFrame(step);
}

btnZero.addEventListener('click', () => {
  targetAngle = 0.0;
  actualAngle = 0.0;
  continuousTurns = 0;
  if (isConnected) {
    sendSerialCommand('ZERO\n');
  }
  updateVisuals();
});

btnMotorDir.addEventListener('click', () => {
  invertMotor = !invertMotor;
  motorDirText.textContent = invertMotor ? '⇄ Motor Dir: Reversed' : '⇄ Motor Dir: Normal';
  btnMotorDir.classList.toggle('active', invertMotor);
  if (isConnected) {
    scheduleSerialSend(targetAngle);
  }
});

btnEncoderDir.addEventListener('click', () => {
  invertEncoder = !invertEncoder;
  encoderDirText.textContent = invertEncoder ? '⇄ Encoder Dir: Inverted (Match)' : '⇄ Encoder Dir: Normal';
  btnEncoderDir.classList.toggle('active', invertEncoder);
  updateVisuals();
});

// ============================================================
// 6. WEB SERIAL API (Direct 1-Click Connect to Pico 2 W @ COM3)
// ============================================================
async function connectSerial() {
  if (!('serial' in navigator)) {
    alert('Web Serial API is supported in Chrome, Edge, and Opera.\nIf using another browser, please run the local Python bridge.');
    return;
  }

  try {
    // Request port or find Pico
    port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });

    isConnected = true;
    connStatus.className = 'status-pill connected';
    connStatus.querySelector('.status-text').textContent = 'PICO 2 W (ONLINE)';
    btnConnect.innerHTML = `<span>Disconnect</span>`;

    readSerialLoop();
  } catch (err) {
    console.error('Serial connection error:', err);
  }
}

async function disconnectSerial() {
  try {
    if (reader) {
      await reader.cancel();
      reader.releaseLock();
      reader = null;
    }
    if (writer) {
      writer.releaseLock();
      writer = null;
    }
    if (port) {
      await port.close();
      port = null;
    }
  } catch (e) {
    console.warn(e);
  }
  isConnected = false;
  connStatus.className = 'status-pill disconnected';
  connStatus.querySelector('.status-text').textContent = 'DISCONNECTED';
  btnConnect.innerHTML = `<svg class="icon-usb" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M5 7l7-5 7 5M5 17l7 5 7-5"/></svg> Connect Pico`;
}

btnConnect.addEventListener('click', () => {
  if (isConnected) {
    disconnectSerial();
  } else {
    connectSerial();
  }
});

// Throttled Serial Transmission (30 Hz)
function scheduleSerialSend(angle) {
  pendingSendAngle = angle;
  if (!serialSendTimer) {
    serialSendTimer = setTimeout(() => {
      if (pendingSendAngle !== null && isConnected && port) {
        const valToSend = invertMotor ? -pendingSendAngle : pendingSendAngle;
        sendSerialCommand(`SET:${valToSend.toFixed(2)}\n`);
        pendingSendAngle = null;
      }
      serialSendTimer = null;
    }, 33);
  }
}

async function sendSerialCommand(cmd) {
  if (!port || !port.writable) return;
  try {
    const encoder = new TextEncoder();
    writer = port.writable.getWriter();
    await writer.write(encoder.encode(cmd));
    writer.releaseLock();
    writer = null;
  } catch (e) {
    console.error('Serial write error:', e);
  }
}

// Receive Serial Telemetry Stream from Pico
async function readSerialLoop() {
  const textDecoder = new TextDecoderStream();
  const readableStreamClosed = port.readable.pipeTo(textDecoder.writable);
  reader = textDecoder.readable.getReader();

  let lineBuffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) {
        lineBuffer += value;
        const lines = lineBuffer.split('\n');
        lineBuffer = lines.pop(); // keep partial line

        for (const line of lines) {
          handleSerialLine(line.trim());
        }
      }
    }
  } catch (err) {
    console.error('Serial read loop error:', err);
  } finally {
    reader.releaseLock();
  }
}

function handleSerialLine(line) {
  // Format: "DATA:<target>,<actual>,<rpm>"
  if (line.startsWith('DATA:')) {
    const parts = line.substring(5).split(',');
    if (parts.length >= 3) {
      const pActual = parseFloat(parts[1]);
      const pRpm = parseFloat(parts[2]);

      if (!isNaN(pActual)) {
        actualAngle = invertEncoder ? -pActual : pActual;
      }
      if (!isNaN(pRpm)) {
        motorRpm = invertEncoder ? -pRpm : pRpm;
      }
      updateVisuals();
    }
  }
}

// ============================================================
// 7. DEMO / SIMULATION FALLBACK (When disconnected)
// ============================================================
// Smoothly simulates physical motor tracking the target when disconnected so the user can see the effect
let simAnimFrame = null;
function runSimulationLoop() {
  if (!isConnected) {
    const diff = targetAngle - actualAngle;
    // Simulate real motor inertia and acceleration
    actualAngle += diff * 0.12;
    motorRpm = diff * 1.8;
    updateVisuals();
  }
  simAnimFrame = requestAnimationFrame(runSimulationLoop);
}

// Init
generateDialTicks();
updateVisuals();
runSimulationLoop();
