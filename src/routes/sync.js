const express = require('express');
const { SyncOperation, File, Device, User } = require('../config/database');
const { RedisService } = require('../config/redis');
const logger = require('../config/logger');

const router = express.Router();

// =============================================================================
// SYNC STATUS ENDPOINTS
// =============================================================================

// Get sync status for user
router.get('/status', async (req, res) => {
    try {
        const userId = req.user.id;

        // Get pending sync operations
        const pendingOperations = await SyncOperation.findAll({
            where: {
                user_id: userId,
                status: ['PENDING', 'IN_PROGRESS']
            },
            include: [{
                model: File,
                as: 'file',
                attributes: ['id', 'filename', 'file_size']
            }],
            order: [['created_at', 'DESC']],
            limit: 10
        });

        // Get recent completed operations
        const recentOperations = await SyncOperation.findAll({
            where: {
                user_id: userId,
                status: 'COMPLETED'
            },
            include: [{
                model: File,
                as: 'file',
                attributes: ['id', 'filename', 'file_size']
            }],
            order: [['completed_at', 'DESC']],
            limit: 5
        });

        // Get user devices and their online status
        const devices = await Device.findActiveDevicesForUser(userId);
        const onlineDevices = devices.filter(device => device.isOnline());

        // Calculate sync statistics
        const stats = {
            totalFiles: await File.count({
                where: {
                    user_id: userId,
                    is_deleted: false
                }
            }),
            completedFiles: await File.count({
                where: {
                    user_id: userId,
                    backup_status: 'COMPLETED',
                    is_deleted: false
                }
            }),
            pendingUploads: pendingOperations.filter(op => op.operation_type === 'UPLOAD').length,
            failedOperations: await SyncOperation.count({
                where: {
                    user_id: userId,
                    status: 'FAILED'
                }
            }),
            lastSync: recentOperations.length > 0 ? recentOperations[0].completed_at : null
        };

        res.json({
            status: 'connected',
            statistics: stats,
            pendingOperations: pendingOperations.map(op => ({
                id: op.id,
                type: op.operation_type,
                status: op.status,
                progress: op.progress,
                file: op.file,
                created_at: op.created_at
            })),
            recentOperations: recentOperations.map(op => ({
                id: op.id,
                type: op.operation_type,
                file: op.file,
                completed_at: op.completed_at
            })),
            devices: {
                total: devices.length,
                online: onlineDevices.length,
                list: devices.map(device => ({
                    id: device.id,
                    name: device.device_name,
                    type: device.device_type,
                    isOnline: device.isOnline(),
                    lastSeen: device.last_seen_at
                }))
            }
        });

    } catch (error) {
        logger.error('Get sync status error:', error);
        res.status(500).json({
            error: 'Failed to get sync status',
            message: 'An internal server error occurred'
        });
    }
});

// Get sync operations history
router.get('/history', async (req, res) => {
    try {
        const { limit = 50, offset = 0, operation_type, status, device_id } = req.query;

        const where = {
            user_id: req.user.id
        };

        if (operation_type) {
            where.operation_type = operation_type;
        }

        if (status) {
            where.status = status;
        }

        if (device_id) {
            where.device_id = device_id;
        }

        const operations = await SyncOperation.findAndCountAll({
            where,
            include: [{
                model: File,
                as: 'file',
                attributes: ['id', 'filename', 'file_size', 'mime_type']
            }, {
                model: Device,
                as: 'device',
                attributes: ['id', 'device_name', 'device_type']
            }],
            limit: parseInt(limit),
            offset: parseInt(offset),
            order: [['created_at', 'DESC']]
        });

        res.json({
            operations: operations.rows,
            total: operations.count,
            limit: parseInt(limit),
            offset: parseInt(offset)
        });

    } catch (error) {
        logger.error('Get sync history error:', error);
        res.status(500).json({
            error: 'Failed to get sync history',
            message: 'An internal server error occurred'
        });
    }
});

// Retry failed sync operation
router.post('/retry/:operationId', async (req, res) => {
    try {
        const { operationId } = req.params;

        const operation = await SyncOperation.findOne({
            where: {
                id: operationId,
                user_id: req.user.id,
                status: 'FAILED'
            }
        });

        if (!operation) {
            return res.status(404).json({
                error: 'Operation not found',
                message: 'Sync operation not found or cannot be retried'
            });
        }

        if (!operation.canRetry()) {
            return res.status(400).json({
                error: 'Cannot retry',
                message: 'Operation has exceeded maximum retry attempts'
            });
        }

        // Reset operation status
        operation.status = 'PENDING';
        operation.error_message = null;
        operation.progress = 0;
        await operation.save();

        // TODO: Add operation back to processing queue
        
        logger.info('Sync operation retry requested', {
            operationId,
            userId: req.user.id,
            operationType: operation.operation_type
        });

        res.json({
            message: 'Operation queued for retry',
            operationId: operation.id,
            status: operation.status
        });

    } catch (error) {
        logger.error('Retry sync operation error:', error);
        res.status(500).json({
            error: 'Failed to retry operation',
            message: 'An internal server error occurred'
        });
    }
});

// Cancel pending sync operation
router.delete('/cancel/:operationId', async (req, res) => {
    try {
        const { operationId } = req.params;

        const operation = await SyncOperation.findOne({
            where: {
                id: operationId,
                user_id: req.user.id,
                status: ['PENDING', 'IN_PROGRESS']
            }
        });

        if (!operation) {
            return res.status(404).json({
                error: 'Operation not found',
                message: 'Sync operation not found or cannot be cancelled'
            });
        }

        operation.status = 'CANCELLED';
        await operation.save();

        // TODO: Remove operation from processing queue

        logger.info('Sync operation cancelled', {
            operationId,
            userId: req.user.id,
            operationType: operation.operation_type
        });

        res.json({
            message: 'Operation cancelled successfully',
            operationId: operation.id
        });

    } catch (error) {
        logger.error('Cancel sync operation error:', error);
        res.status(500).json({
            error: 'Failed to cancel operation',
            message: 'An internal server error occurred'
        });
    }
});

// Force sync for a device
router.post('/force/:deviceId?', async (req, res) => {
    try {
        const deviceId = req.params.deviceId || req.device?.id;
        
        if (!deviceId) {
            return res.status(400).json({
                error: 'Device ID required',
                message: 'Please specify a device ID or ensure device is properly authenticated'
            });
        }

        const device = await Device.findOne({
            where: {
                id: deviceId,
                user_id: req.user.id,
                is_active: true
            }
        });

        if (!device) {
            return res.status(404).json({
                error: 'Device not found',
                message: 'Device not found or not active'
            });
        }

        // Update device last sync time
        device.last_sync_at = new Date();
        await device.save();

        // TODO: Trigger sync operations for the device
        // This would involve checking for files that need to be synced
        // and creating appropriate sync operations

        logger.info('Force sync requested', {
            deviceId,
            userId: req.user.id,
            deviceName: device.device_name
        });

        res.json({
            message: 'Sync initiated successfully',
            deviceId: device.id,
            deviceName: device.device_name,
            syncTime: device.last_sync_at
        });

    } catch (error) {
        logger.error('Force sync error:', error);
        res.status(500).json({
            error: 'Failed to initiate sync',
            message: 'An internal server error occurred'
        });
    }
});

// Get sync settings for current device
router.get('/settings', async (req, res) => {
    try {
        const device = await Device.findByPk(req.device?.id);
        
        if (!device) {
            return res.status(404).json({
                error: 'Device not found',
                message: 'Device information not available'
            });
        }

        res.json({
            deviceId: device.id,
            deviceName: device.device_name,
            syncSettings: device.sync_settings,
            backupDirectories: device.backup_directories,
            lastBackup: device.last_backup_at,
            lastSync: device.last_sync_at
        });

    } catch (error) {
        logger.error('Get sync settings error:', error);
        res.status(500).json({
            error: 'Failed to get sync settings',
            message: 'An internal server error occurred'
        });
    }
});

// Update sync settings for current device
router.put('/settings', async (req, res) => {
    try {
        const { syncSettings, backupDirectories } = req.body;
        
        const device = await Device.findByPk(req.device?.id);
        
        if (!device) {
            return res.status(404).json({
                error: 'Device not found',
                message: 'Device information not available'
            });
        }

        // Update sync settings
        if (syncSettings) {
            device.sync_settings = { ...device.sync_settings, ...syncSettings };
        }

        // Update backup directories
        if (Array.isArray(backupDirectories)) {
            device.backup_directories = backupDirectories;
        }

        await device.save();

        logger.info('Sync settings updated', {
            deviceId: device.id,
            userId: req.user.id,
            settings: { syncSettings, backupDirectories }
        });

        res.json({
            message: 'Sync settings updated successfully',
            deviceId: device.id,
            syncSettings: device.sync_settings,
            backupDirectories: device.backup_directories
        });

    } catch (error) {
        logger.error('Update sync settings error:', error);
        res.status(500).json({
            error: 'Failed to update sync settings',
            message: 'An internal server error occurred'
        });
    }
});

module.exports = router;