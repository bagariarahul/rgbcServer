const express = require('express');
const router = express.Router();
const os = require('os');
const { execSync } = require('child_process');
const logger = require('../config/logger');

/**
 * GET /api/server-info
 *
 * Returns server diagnostics: uptime, OS details, memory, and
 * disk space for the storage volume.
 *
 * No authentication required — this is a health/status endpoint.
 * If you want to restrict it, add your auth middleware before the handler.
 */
router.get('/', (req, res) => {
    try {
        const uptimeSeconds = os.uptime();

        // ── Disk space (Linux: df on /app/storage or /) ─────────────
        let disk = { total: 0, free: 0, used: 0, usedPercent: 0 };
        try {
            // Target the storage volume; fall back to root
            const storagePath = process.env.UPLOAD_PATH || '/';
            const dfOutput = execSync(`df -B1 ${storagePath} | tail -1`).toString().trim();
            const parts = dfOutput.split(/\s+/);
            // df columns: Filesystem 1B-blocks Used Available Use% Mounted
            if (parts.length >= 5) {
                const total = parseInt(parts[1], 10) || 0;
                const used  = parseInt(parts[2], 10) || 0;
                const free  = parseInt(parts[3], 10) || 0;
                disk = {
                    total,
                    free,
                    used,
                    usedPercent: total > 0 ? Math.round((used / total) * 100) : 0
                };
            }
        } catch (e) {
            logger.warn('Disk space check failed (non-Linux?): ' + e.message);
        }

        // ── Memory ──────────────────────────────────────────────────
        const totalMem = os.totalmem();
        const freeMem  = os.freemem();

        const payload = {
            status: 'online',
            uptime: {
                seconds: Math.floor(uptimeSeconds),
                formatted: formatUptime(uptimeSeconds)
            },
            os: {
                type: os.type(),       // Linux
                platform: os.platform(), // linux
                release: os.release(),   // 5.15.0-...
                arch: os.arch(),         // x64 / arm64
                hostname: os.hostname()
            },
            memory: {
                total: totalMem,
                free: freeMem,
                used: totalMem - freeMem,
                usedPercent: Math.round(((totalMem - freeMem) / totalMem) * 100)
            },
            disk,
            node: {
                version: process.version,
                env: process.env.NODE_ENV || 'development'
            },
            timestamp: new Date().toISOString()
        };

        res.json(payload);

    } catch (error) {
        logger.error('Server info endpoint error:', error);
        res.status(500).json({
            status: 'error',
            message: 'Failed to retrieve server information'
        });
    }
});

/**
 * Format seconds into "Xd Xh Xm" string.
 */
function formatUptime(seconds) {
    const days    = Math.floor(seconds / 86400);
    const hours   = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    const parts = [];
    if (days > 0)    parts.push(`${days}d`);
    if (hours > 0)   parts.push(`${hours}h`);
    if (minutes > 0) parts.push(`${minutes}m`);

    return parts.length > 0 ? parts.join(' ') : '< 1m';
}

module.exports = router;