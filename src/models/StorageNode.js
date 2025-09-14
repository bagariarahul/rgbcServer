const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const StorageNode = sequelize.define('storage_nodes', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
        },
        node_name: {
            type: DataTypes.STRING(100),
            allowNull: false,
            unique: true
        },
        node_type: {
            type: DataTypes.ENUM('PRIMARY', 'SECONDARY', 'BACKUP', 'ARCHIVE'),
            defaultValue: 'PRIMARY'
        },
        storage_path: {
            type: DataTypes.TEXT,
            allowNull: false,
            comment: 'Absolute path to storage directory'
        },
        mount_point: {
            type: DataTypes.STRING(255),
            allowNull: true,
            comment: 'System mount point if applicable'
        },
        available_space: {
            type: DataTypes.BIGINT,
            allowNull: false,
            defaultValue: 0,
            comment: 'Available space in bytes'
        },
        total_space: {
            type: DataTypes.BIGINT,
            allowNull: false,
            defaultValue: 0,
            comment: 'Total space in bytes'
        },
        used_space: {
            type: DataTypes.BIGINT,
            allowNull: false,
            defaultValue: 0,
            comment: 'Used space in bytes'
        },
        raid_level: {
            type: DataTypes.STRING(10),
            allowNull: true,
            comment: 'RAID level: 0, 1, 5, 6, 10, etc.'
        },
        raid_config: {
            type: DataTypes.JSON,
            allowNull: true,
            comment: 'RAID configuration details'
        },
        status: {
            type: DataTypes.ENUM('ACTIVE', 'INACTIVE', 'MAINTENANCE', 'FAILED', 'DEGRADED'),
            defaultValue: 'ACTIVE'
        },
        health_score: {
            type: DataTypes.INTEGER,
            allowNull: true,
            defaultValue: 100,
            validate: {
                min: 0,
                max: 100
            },
            comment: 'Health score from 0-100'
        },
        io_stats: {
            type: DataTypes.JSON,
            allowNull: true,
            comment: 'I/O performance statistics'
        },
        last_health_check: {
            type: DataTypes.DATE,
            allowNull: true
        },
        last_error: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        error_count: {
            type: DataTypes.INTEGER,
            defaultValue: 0
        },
        priority: {
            type: DataTypes.INTEGER,
            defaultValue: 100,
            comment: 'Storage priority for allocation'
        },
        max_file_size: {
            type: DataTypes.BIGINT,
            allowNull: true,
            comment: 'Maximum file size in bytes'
        },
        compression_enabled: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        encryption_enabled: {
            type: DataTypes.BOOLEAN,
            defaultValue: true
        },
        backup_schedule: {
            type: DataTypes.JSON,
            allowNull: true,
            comment: 'Backup schedule configuration'
        }
    }, {
        indexes: [
            {
                fields: ['node_name']
            },
            {
                fields: ['status']
            },
            {
                fields: ['node_type']
            },
            {
                fields: ['priority']
            },
            {
                fields: ['last_health_check']
            }
        ]
    });

    // Instance methods
    StorageNode.prototype.isHealthy = function() {
        return this.status === 'ACTIVE' && (this.health_score || 0) >= 80;
    };

    StorageNode.prototype.getUsagePercentage = function() {
        if (this.total_space === 0) return 0;
        return Math.round((this.used_space / this.total_space) * 100);
    };

    StorageNode.prototype.hasAvailableSpace = function(requiredBytes = 0) {
        return this.available_space >= requiredBytes;
    };

    StorageNode.prototype.updateSpaceUsage = function(usedBytes, totalBytes, availableBytes) {
        this.used_space = usedBytes;
        this.total_space = totalBytes;
        this.available_space = availableBytes;
        return this.save();
    };

    StorageNode.prototype.recordHealthCheck = function(healthScore, ioStats = null) {
        this.health_score = healthScore;
        this.last_health_check = new Date();
        if (ioStats) {
            this.io_stats = ioStats;
        }
        
        // Update status based on health score
        if (healthScore >= 90) {
            this.status = 'ACTIVE';
        } else if (healthScore >= 70) {
            this.status = 'DEGRADED';
        } else {
            this.status = 'FAILED';
        }
        
        return this.save();
    };

    StorageNode.prototype.recordError = function(errorMessage) {
        this.last_error = errorMessage;
        this.error_count += 1;
        this.health_score = Math.max(0, (this.health_score || 100) - 10);
        
        // Set to failed if too many errors
        if (this.error_count >= 5) {
            this.status = 'FAILED';
        }
        
        return this.save();
    };

    StorageNode.prototype.resetErrors = function() {
        this.error_count = 0;
        this.last_error = null;
        this.health_score = Math.min(100, (this.health_score || 0) + 20);
        
        if (this.status === 'FAILED' && this.health_score >= 80) {
            this.status = 'ACTIVE';
        }
        
        return this.save();
    };

    // Static methods
    StorageNode.getActiveNodes = function() {
        return this.findAll({
            where: {
                status: ['ACTIVE', 'DEGRADED']
            },
            order: [['priority', 'DESC'], ['health_score', 'DESC']]
        });
    };

    StorageNode.getBestNodeForStorage = function(requiredSpace = 0) {
        return this.findOne({
            where: {
                status: 'ACTIVE',
                available_space: {
                    [sequelize.Sequelize.Op.gte]: requiredSpace
                }
            },
            order: [['priority', 'DESC'], ['health_score', 'DESC'], ['available_space', 'DESC']]
        });
    };

    StorageNode.getStorageStatistics = async function() {
        const nodes = await this.findAll();
        
        const stats = {
            total_nodes: nodes.length,
            active_nodes: 0,
            failed_nodes: 0,
            total_capacity: 0,
            used_capacity: 0,
            available_capacity: 0,
            average_health: 0
        };

        let healthSum = 0;
        
        nodes.forEach(node => {
            if (node.status === 'ACTIVE') {
                stats.active_nodes++;
            } else if (node.status === 'FAILED') {
                stats.failed_nodes++;
            }
            
            stats.total_capacity += node.total_space;
            stats.used_capacity += node.used_space;
            stats.available_capacity += node.available_space;
            healthSum += (node.health_score || 0);
        });

        stats.average_health = nodes.length > 0 ? Math.round(healthSum / nodes.length) : 0;
        stats.usage_percentage = stats.total_capacity > 0 ? 
            Math.round((stats.used_capacity / stats.total_capacity) * 100) : 0;

        return stats;
    };

    return StorageNode;
};