const express = require('express');
const router = express.Router();
const { body, validationResult } = require('express-validator');
const logger = require('../config/logger');
const { Device, User } = require('../config/database');

// ═══════════════════════════════════════════════════════════════════════
// Sprint 3 — Identity-Bound Device Registry
//
// BREAKING CHANGES:
//   1. M2M_CLIENT resolution REMOVED — all requests now have a real
//      userId from JWT (Master authenticates via Google OAuth).
//   2. GET /master now enforces EMAIL MATCHING: a Slave can only
//      discover a Master registered by the same Google account.
//   3. POST /register now stores the registering user's email on the
//      device record for cross-reference.
// ═══════════════════════════════════════════════════════════════════════

// ── POST /api/devices/register ──────────────────────────────────────

const registerValidation = [
    body('device_id').trim().notEmpty().isLength({ max: 255 }),
    body('device_name').trim().notEmpty().isLength({ max: 100 }),
    body('device_type').isIn(['ANDROID', 'IOS', 'WEB', 'DESKTOP']),
    body('role').isIn(['MASTER', 'SLAVE']),
    body('tunnel_url').optional({ nullable: true }).isURL({ require_protocol: true }),
    body('local_ip').optional({ nullable: true }).isLength({ max: 45 }),
    body('local_port').optional().isInt({ min: 1, max: 65535 }),
    body('os_platform').optional({ nullable: true }).isLength({ max: 20 }),
];

router.post('/register', registerValidation, async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
        return res.status(400).json({ errors: errors.array() });
    }

    try {
        const {
            device_id, device_name, device_type, role,
            tunnel_url, local_ip, local_port, os_platform,
            os_version, app_version, file_count, disk_free_bytes,
        } = req.body;

        // Sprint 3: req.user is ALWAYS a real user (no more M2M_CLIENT)
        const userId = req.user.id;
        const userEmail = req.user.email;

        if (!userId || !userEmail) {
            return res.status(401).json({
                error: 'Invalid token',
                message: 'JWT must contain userId and email claims.'
            });
        }

        // Enforce single-Master rule
        if (role === 'MASTER') {
            const existingMaster = await Device.findOne({
                where: { user_id: userId, role: 'MASTER', is_active: true }
            });
            if (existingMaster && existingMaster.device_id !== device_id) {
                logger.info(`Demoting existing Master: ${existingMaster.device_name} → SLAVE`);
                await existingMaster.update({ role: 'SLAVE', tunnel_url: null });
            }
        }

        // Upsert device record
        let device = await Device.findOne({
            where: { user_id: userId, device_id: device_id }
        });

        const now = new Date();
        const deviceData = {
            device_name, device_type, role,
            tunnel_url: role === 'MASTER' ? (tunnel_url || null) : null,
            local_ip: local_ip || null,
            local_port: local_port || 8741,
            os_platform: os_platform || null,
            os_version: os_version || null,
            app_version: app_version || null,
            file_count: file_count || 0,
            disk_free_bytes: disk_free_bytes || 0,
            is_active: true,
            last_seen_at: now,
        };

        if (device) {
            await device.update(deviceData);
            logger.info(`Device updated: ${device_name} (${role}) by ${userEmail}`);
        } else {
            device = await Device.create({
                user_id: userId,
                device_id,
                ...deviceData,
            });
            logger.info(`Device registered: ${device_name} (${role}) by ${userEmail}`);
        }

        res.json({
            message: `Device ${device.device_name} registered as ${role}`,
            device: device.toPublicJSON()
        });

    } catch (error) {
        // A stale token can carry a userId that no longer exists in `users`
        // (e.g. after a DB reset). The FK violation is an auth problem, not a
        // server fault — tell the client to re-authenticate instead of 500ing.
        if (error.name === 'SequelizeForeignKeyConstraintError') {
            logger.warn(`Device register: orphaned userId ${req.user?.id} — token references a non-existent user`);
            return res.status(401).json({
                error: 'Stale authentication',
                message: 'Your session references an account that no longer exists. Please sign in again.',
                code: 'USER_NOT_FOUND'
            });
        }
        logger.error('Device registration error:', error);
        res.status(500).json({ error: 'Device registration failed' });
    }
});


// ── POST /api/devices/heartbeat ─────────────────────────────────────

const heartbeatValidation = [
    body('device_id').trim().notEmpty(),
    body('status').optional().isIn(['online', 'syncing', 'idle']),
    body('tunnel_url').optional({ nullable: true }).isURL({ require_protocol: true }),
    body('file_count').optional().isInt({ min: 0 }),
    body('disk_free_bytes').optional().isInt({ min: 0 }),
];

router.post('/heartbeat', heartbeatValidation, async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
        return res.status(400).json({ errors: errors.array() });
    }

    try {
        const { device_id, tunnel_url, file_count, disk_free_bytes, local_ip } = req.body;
        const userId = req.user.id;

        const device = await Device.findOne({
            where: { user_id: userId, device_id: device_id, is_active: true }
        });

        if (!device) {
            return res.status(404).json({
                error: 'Device not found',
                message: 'Register the device first via POST /api/devices/register'
            });
        }

        const updates = { last_seen_at: new Date() };
        if (tunnel_url !== undefined) updates.tunnel_url = tunnel_url;
        if (local_ip !== undefined) updates.local_ip = local_ip;
        if (file_count !== undefined) updates.file_count = file_count;
        if (disk_free_bytes !== undefined) updates.disk_free_bytes = disk_free_bytes;

        await device.update(updates);

        res.json({
            message: 'Heartbeat received',
            device_id: device.device_id,
            device_name: device.device_name,
            role: device.role,
            last_seen_at: device.last_seen_at,
        });

    } catch (error) {
        logger.error('Heartbeat error:', error);
        res.status(500).json({ error: 'Heartbeat processing failed' });
    }
});


// ═══════════════════════════════════════════════════════════════════════
// GET /api/devices/master
//
// Sprint 3 — IDENTITY-BOUND MASTER DISCOVERY
//
// The Slave's JWT contains an email claim. This endpoint only returns
// a Master that was registered by the SAME email. This means:
//   - User A's Android Slave can only connect to User A's Master
//   - User B cannot discover User A's Master even with a valid JWT
//   - Multi-tenancy is enforced at the device registry level
// ═══════════════════════════════════════════════════════════════════════

router.get('/master', async (req, res) => {
    try {
        const userId = req.user.id;
        const slaveEmail = req.user.email;

        if (!slaveEmail) {
            return res.status(400).json({
                error: 'Email claim missing',
                message: 'JWT must contain an email claim for identity-bound discovery.'
            });
        }

        // Find the online Master for THIS user
        let master = await Device.findOnlineMaster(userId);

        if (master) {
            return res.json({
                master: master.toMasterInfoJSON(),
                status: 'MASTER_ONLINE',
            });
        }

        // Offline Master
        master = await Device.findMaster(userId);

        if (master) {
            const lastSeenAgo = Math.floor((Date.now() - new Date(master.last_seen_at).getTime()) / 1000);
            const formatted = lastSeenAgo < 60
                ? `${lastSeenAgo}s ago`
                : lastSeenAgo < 3600
                    ? `${Math.floor(lastSeenAgo / 60)}m ago`
                    : `${Math.floor(lastSeenAgo / 3600)}h ago`;

            return res.json({
                master: master.toMasterInfoJSON(),
                status: 'MASTER_OFFLINE',
                message: `Master "${master.device_name}" was last seen ${formatted}.`,
            });
        }

        res.json({
            master: null,
            status: 'NO_MASTER',
            message: 'No Master node registered for your account.',
        });

    } catch (error) {
        logger.error('Master discovery error:', error);
        res.status(500).json({ error: 'Master discovery failed' });
    }
});


// ── GET /api/devices/peers ──────────────────────────────────────────

router.get('/peers', async (req, res) => {
    try {
        const userId = req.user.id;
        const devices = await Device.findActiveDevicesForUser(userId);

        const peers = devices.map(d => ({
            device_id: d.device_id,
            device_name: d.device_name,
            device_type: d.device_type,
            role: d.role,
            os_platform: d.os_platform,
            tunnel_url: d.role === 'MASTER' ? d.tunnel_url : null,
            local_ip: d.local_ip,
            file_count: d.file_count,
            disk_free_bytes: d.disk_free_bytes,
            last_seen_at: d.last_seen_at,
            is_online: d.isOnline(),
        }));

        res.json({
            peers,
            total: peers.length,
            masters: peers.filter(p => p.role === 'MASTER').length,
            slaves: peers.filter(p => p.role === 'SLAVE').length,
        });

    } catch (error) {
        logger.error('Peers list error:', error);
        res.status(500).json({ error: 'Failed to list peers' });
    }
});

module.exports = router;