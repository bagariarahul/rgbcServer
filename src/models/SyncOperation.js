const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const SyncOperation = sequelize.define('sync_operations', {
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
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'devices',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        file_id: {
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'files',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        operation_type: {
            type: DataTypes.ENUM('UPLOAD', 'DOWNLOAD', 'DELETE', 'RENAME', 'MOVE'),
            allowNull: false
        },
        status: {
            type: DataTypes.ENUM('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED'),
            defaultValue: 'PENDING'
        },
        sync_direction: {
            type: DataTypes.ENUM('DEVICE_TO_SERVER', 'SERVER_TO_DEVICE', 'BIDIRECTIONAL'),
            defaultValue: 'DEVICE_TO_SERVER'
        },
        progress: {
            type: DataTypes.INTEGER,
            defaultValue: 0,
            validate: {
                min: 0,
                max: 100
            }
        },
        error_message: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        metadata: {
            type: DataTypes.JSON,
            allowNull: true,
            defaultValue: null
        },
        retry_count: {
            type: DataTypes.INTEGER,
            defaultValue: 0
        },
        max_retries: {
            type: DataTypes.INTEGER,
            defaultValue: 3
        },
        started_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        completed_at: {
            type: DataTypes.DATE,
            allowNull: true
        }
    }, {
        indexes: [
            {
                fields: ['user_id']
            },
            {
                fields: ['device_id']
            },
            {
                fields: ['file_id']
            },
            {
                fields: ['operation_type']
            },
            {
                fields: ['status']
            },
            {
                fields: ['created_at']
            }
        ]
    });

    // Instance methods
    SyncOperation.prototype.isCompleted = function() {
        return this.status === 'COMPLETED';
    };

    SyncOperation.prototype.canRetry = function() {
        return this.status === 'FAILED' && this.retry_count < this.max_retries;
    };

    SyncOperation.prototype.markInProgress = function() {
        this.status = 'IN_PROGRESS';
        this.started_at = new Date();
        return this.save();
    };

    SyncOperation.prototype.markCompleted = function() {
        this.status = 'COMPLETED';
        this.progress = 100;
        this.completed_at = new Date();
        return this.save();
    };

    SyncOperation.prototype.markFailed = function(errorMessage) {
        this.status = 'FAILED';
        this.error_message = errorMessage;
        this.retry_count += 1;
        return this.save();
    };

    return SyncOperation;
};