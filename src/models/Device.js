const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const Device = sequelize.define('devices', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
        },
        user_id: {
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'users',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        device_id: {
            type: DataTypes.STRING(255),
            allowNull: false,
            comment: 'Client-generated device identifier'
        },
        device_name: {
            type: DataTypes.STRING(100),
            allowNull: false,
            validate: {
                len: [1, 100],
                notEmpty: true
            }
        },
        device_type: {
            type: DataTypes.ENUM('ANDROID', 'IOS', 'WEB', 'DESKTOP'),
            allowNull: false
        },
        device_fingerprint: {
            type: DataTypes.STRING(64),
            allowNull: true,
            comment: 'Hash of device characteristics for security'
        },
        os_version: {
            type: DataTypes.STRING(50),
            allowNull: true
        },
        app_version: {
            type: DataTypes.STRING(20),
            allowNull: true
        },
        push_token: {
            type: DataTypes.TEXT,
            allowNull: true,
            comment: 'FCM or APNS token for push notifications'
        },
        is_active: {
            type: DataTypes.BOOLEAN,
            defaultValue: true
        },
        last_seen_at: {
            type: DataTypes.DATE,
            defaultValue: DataTypes.NOW
        },
        sync_settings: {
            type: DataTypes.JSON,
            defaultValue: {
                auto_backup: true,
                wifi_only: false,
                backup_photos: true,
                backup_videos: true,
                backup_documents: true,
                backup_schedule: 'REAL_TIME',
                chunk_size: 5242880,
                parallel_uploads: 3,
                compress_uploads: false
            }
        },
        backup_directories: {
            type: DataTypes.JSON,
            defaultValue: [],
            comment: 'Array of directory paths to backup'
        },
        last_backup_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        last_sync_at: {
            type: DataTypes.DATE,
            allowNull: true
        },

        // ═══════════════════════════════════════════════════════════
        // SPRINT 2: P2P Master-Slave signaling columns
        // ═══════════════════════════════════════════════════════════

        role: {
            type: DataTypes.STRING(10),
            defaultValue: 'SLAVE',
            allowNull: false,
            validate: {
                isIn: [['MASTER', 'SLAVE']]
            },
            comment: 'MASTER stores files and runs local API; SLAVE pushes/pulls to Master'
        },
        tunnel_url: {
            type: DataTypes.TEXT,
            allowNull: true,
            comment: 'Cloudflare Tunnel URL for Master nodes (e.g., https://master-abc.bagariaa.in)'
        },
        local_ip: {
            type: DataTypes.STRING(45),
            allowNull: true,
            comment: 'LAN IPv4/IPv6 for same-network direct connect optimization'
        },
        local_port: {
            type: DataTypes.INTEGER,
            defaultValue: 8741,
            comment: 'FastAPI port on Master node'
        },
        file_count: {
            type: DataTypes.INTEGER,
            defaultValue: 0,
            comment: 'Number of files tracked by this device'
        },
        disk_free_bytes: {
            type: DataTypes.BIGINT,
            defaultValue: 0,
            comment: 'Free disk space reported via heartbeat'
        },
        os_platform: {
            type: DataTypes.STRING(20),
            allowNull: true,
            comment: 'win32, darwin, linux, android, ios'
        }
    }, {
        indexes: [
            {
                unique: true,
                fields: ['user_id', 'device_id']
            },
            {
                fields: ['user_id']
            },
            {
                fields: ['device_type']
            },
            {
                fields: ['is_active']
            },
            {
                fields: ['last_seen_at']
            },
            // Sprint 2: index for fast Master lookup
            {
                fields: ['role']
            },
            {
                fields: ['role', 'is_active', 'last_seen_at']
            }
        ]
    });

    // ── Instance methods ────────────────────────────────────────────

    Device.prototype.updateLastSeen = function() {
        this.last_seen_at = new Date();
        return this.save();
    };

    Device.prototype.isOnline = function() {
        const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000);
        return this.last_seen_at > fiveMinutesAgo;
    };

    Device.prototype.isMaster = function() {
        return this.role === 'MASTER';
    };

    Device.prototype.updateSyncSettings = function(settings) {
        this.sync_settings = { ...this.sync_settings, ...settings };
        return this.save();
    };

    Device.prototype.addBackupDirectory = function(directoryPath) {
        const directories = this.backup_directories || [];
        if (!directories.includes(directoryPath)) {
            directories.push(directoryPath);
            this.backup_directories = directories;
            return this.save();
        }
        return Promise.resolve(this);
    };

    Device.prototype.removeBackupDirectory = function(directoryPath) {
        const directories = this.backup_directories || [];
        const index = directories.indexOf(directoryPath);
        if (index > -1) {
            directories.splice(index, 1);
            this.backup_directories = directories;
            return this.save();
        }
        return Promise.resolve(this);
    };

    Device.prototype.shouldBackupFile = function(filePath, mimeType) {
        const settings = this.sync_settings;
        if (!settings.auto_backup) return false;
        if (mimeType.startsWith('image/') && !settings.backup_photos) return false;
        if (mimeType.startsWith('video/') && !settings.backup_videos) return false;
        if (mimeType.startsWith('application/') && !settings.backup_documents) return false;
        if (mimeType.startsWith('text/') && !settings.backup_documents) return false;
        const directories = this.backup_directories || [];
        if (directories.length === 0) return true;
        return directories.some(dir => filePath.startsWith(dir));
    };

    Device.prototype.toPublicJSON = function() {
        const device = this.toJSON();
        delete device.device_fingerprint;
        delete device.push_token;
        return device;
    };

    // Sprint 2: Safe public representation for Slave discovery
    Device.prototype.toMasterInfoJSON = function() {
        return {
            device_id: this.device_id,
            device_name: this.device_name,
            device_type: this.device_type,
            role: this.role,
            tunnel_url: this.tunnel_url,
            local_ip: this.local_ip,
            local_port: this.local_port,
            file_count: this.file_count,
            disk_free_bytes: this.disk_free_bytes,
            os_platform: this.os_platform,
            last_seen_at: this.last_seen_at,
            is_online: this.isOnline()
        };
    };

    // ── Static methods ──────────────────────────────────────────────

    Device.findByUserAndDeviceId = function(userId, deviceId) {
        return this.findOne({
            where: {
                user_id: userId,
                device_id: deviceId,
                is_active: true
            }
        });
    };

    Device.findActiveDevicesForUser = function(userId) {
        return this.findAll({
            where: {
                user_id: userId,
                is_active: true
            },
            order: [['last_seen_at', 'DESC']]
        });
    };

    Device.findOnlineDevicesForUser = function(userId) {
        const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000);
        return this.findAll({
            where: {
                user_id: userId,
                is_active: true,
                last_seen_at: {
                    [sequelize.Sequelize.Op.gt]: fiveMinutesAgo
                }
            },
            order: [['last_seen_at', 'DESC']]
        });
    };

    // Sprint 2: Find the online Master for a given user
    Device.findOnlineMaster = function(userId) {
        const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000);
        return this.findOne({
            where: {
                user_id: userId,
                role: 'MASTER',
                is_active: true,
                last_seen_at: {
                    [sequelize.Sequelize.Op.gt]: fiveMinutesAgo
                }
            },
            order: [['last_seen_at', 'DESC']]
        });
    };

    // Sprint 2: Find ANY registered Master (online or not)
    Device.findMaster = function(userId) {
        return this.findOne({
            where: {
                user_id: userId,
                role: 'MASTER',
                is_active: true
            },
            order: [['last_seen_at', 'DESC']]
        });
    };

    return Device;
};