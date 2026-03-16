const express = require('express');
const router = express.Router();
const { body, validationResult } = require('express-validator');
const logger = require('../config/logger');
const { Device, User } = require('../config/database');

// ═══════════════════════════════════════════════════════════════════════
// POST /api/devices/register
//
// Called by Master and Slave nodes on startup.
// Upserts the device record and assigns/updates the role.
//
// Auth: verifyToken middleware (JWT or M2M API key)
// ═══════════════════════════════════════════════════════════════════════

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
            device_id,
            device_name,
            device_type,
            role,
            tunnel_url,
            local_ip,
            local_port,
            os_platform,
            os_version,
            app_version,
            file_count,
            disk_free_bytes,
        } = req.body;

        // req.user is set by verifyToken middleware
        // For M2M (API key), user.id is 'M2M_CLIENT' — we need a real user_id.
        // Find the admin user by the ALLOWED_ADMIN_EMAIL.
        let userId = req.user.id;
        if (userId === 'M2M_CLIENT') {
            const adminEmail = process.env.ALLOWED_ADMIN_EMAIL;
            if (!adminEmail) {
                return res.status(500).json({
                    error: 'ALLOWED_ADMIN_EMAIL not configured on server'
                });
            }
            const adminUser = await User.findOne({ where: { email: adminEmail } });
            if (!adminUser) {
                return res.status(404).json({
                    error: 'Admin user not found. Log in via Google OAuth first to create the user record.'
                });
            }
            userId = adminUser.id;
        }

        // ── Enforce single-Master rule ──────────────────────────────
        // Only one device per user can be MASTER at a time.
        if (role === 'MASTER') {
            const existingMaster = await Device.findOne({
                where: {
                    user_id: userId,
                    role: 'MASTER',
                    is_active: true
                }
            });

            // If a different device is already Master, demote it
            if (existingMaster && existingMaster.device_id !== device_id) {
                logger.info(`Demoting existing Master: ${existingMaster.device_name} → SLAVE`);
                await existingMaster.update({
                    role: 'SLAVE',
                    tunnel_url: null
                });
            }
        }

        // ── Upsert device record ────────────────────────────────────
        let device = await Device.findOne({
            where: { user_id: userId, device_id: device_id }
        });

        const now = new Date();

        if (device) {
            // Update existing device
            await device.update({
                device_name,
                device_type,
                role,
                tunnel_url: role === 'MASTER' ? (tunnel_url || null) : null,
                local_ip: local_ip || null,
                local_port: local_port || 8741,
                os_platform: os_platform || null,
                os_version: os_version || device.os_version,
                app_version: app_version || device.app_version,
                file_count: file_count || device.file_count || 0,
                disk_free_bytes: disk_free_bytes || device.disk_free_bytes || 0,
                is_active: true,
                last_seen_at: now,
            });
            logger.info(`Device updated: ${device_name} (${role})`);
        } else {
            // Create new device
            device = await Device.create({
                user_id: userId,
                device_id,
                device_name,
                device_type,
                role,
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
            });
            logger.info(`Device registered: ${device_name} (${role})`);
        }

        res.json({
            message: `Device ${device.device_name} registered as ${role}`,
            device: device.toPublicJSON()
        });

    } catch (error) {
        logger.error('Device registration error:', error);
        res.status(500).json({ error: 'Device registration failed' });
    }
});


// ═══════════════════════════════════════════════════════════════════════
// POST /api/devices/heartbeat
//
// Called by the Master Node every 30 seconds to report liveness.
// Updates last_seen_at, tunnel_url, file_count, disk_free_bytes.
//
// Auth: verifyToken middleware
// ═══════════════════════════════════════════════════════════════════════

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

        // Resolve user ID (same M2M logic as register)
        let userId = req.user.id;
        if (userId === 'M2M_CLIENT') {
            const adminEmail = process.env.ALLOWED_ADMIN_EMAIL;
            const adminUser = adminEmail
                ? await User.findOne({ where: { email: adminEmail } })
                : null;
            if (!adminUser) {
                return res.status(404).json({ error: 'Admin user not found' });
            }
            userId = adminUser.id;
        }

        const device = await Device.findOne({
            where: { user_id: userId, device_id: device_id, is_active: true }
        });

        if (!device) {
            return res.status(404).json({
                error: 'Device not found',
                message: 'Register the device first via POST /api/devices/register'
            });
        }

        // Build update payload — only update fields that were sent
        const updates = {
            last_seen_at: new Date(),
        };

        if (tunnel_url !== undefined) updates.tunnel_url = tunnel_url;
        if (local_ip !== undefined)   updates.local_ip = local_ip;
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
// Called by Slave nodes (Android/iOS/Python) to discover the Master.
// Returns the Master's tunnel_url, local_ip, and online status.
//
// Auth: verifyToken middleware
// ═══════════════════════════════════════════════════════════════════════

router.get('/master', async (req, res) => {
    try {
        // Resolve user ID
        let userId = req.user.id;
        if (userId === 'M2M_CLIENT') {
            const adminEmail = process.env.ALLOWED_ADMIN_EMAIL;
            const adminUser = adminEmail
                ? await User.findOne({ where: { email: adminEmail } })
                : null;
            if (!adminUser) {
                return res.status(404).json({ error: 'Admin user not found' });
            }
            userId = adminUser.id;
        }

        // Try to find an online Master first (seen in last 5 minutes)
        let master = await Device.findOnlineMaster(userId);

        if (master) {
            return res.json({
                master: master.toMasterInfoJSON(),
                status: 'MASTER_ONLINE',
            });
        }

        // No online Master — check if one exists but is offline
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
                message: `Master "${master.device_name}" was last seen ${formatted}. Files will sync when it comes back online.`,
            });
        }

        // No Master registered at all
        res.json({
            master: null,
            status: 'NO_MASTER',
            message: 'No Master node has been registered. Run the RGBC Drive desktop client as Master first.',
        });

    } catch (error) {
        logger.error('Master discovery error:', error);
        res.status(500).json({ error: 'Master discovery failed' });
    }
});


// ═══════════════════════════════════════════════════════════════════════
// GET /api/devices/peers
//
// Returns all registered devices for the authenticated user.
// Useful for a "My Devices" dashboard.
//
// Auth: verifyToken middleware
// ═══════════════════════════════════════════════════════════════════════

router.get('/peers', async (req, res) => {
    try {
        let userId = req.user.id;
        if (userId === 'M2M_CLIENT') {
            const adminEmail = process.env.ALLOWED_ADMIN_EMAIL;
            const adminUser = adminEmail
                ? await User.findOne({ where: { email: adminEmail } })
                : null;
            if (!adminUser) {
                return res.status(404).json({ error: 'Admin user not found' });
            }
            userId = adminUser.id;
        }

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