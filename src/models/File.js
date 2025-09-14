const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const File = sequelize.define('files', {
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
        file_path: {
            type: DataTypes.TEXT,
            allowNull: false,
            comment: 'Original path on the device'
        },
        filename: {
            type: DataTypes.STRING(255),
            allowNull: false
        },
        file_size: {
            type: DataTypes.BIGINT,
            allowNull: false,
            defaultValue: 0
        },
        mime_type: {
            type: DataTypes.STRING(100),
            allowNull: false
        },
        file_hash: {
            type: DataTypes.STRING(64),
            allowNull: false,
            comment: 'SHA-256 hash for deduplication'
        },
        storage_path: {
            type: DataTypes.TEXT,
            allowNull: false,
            comment: 'Path where file is stored on server'
        },
        encryption_key_id: {
            type: DataTypes.UUID,
            allowNull: true,
            references: {
                model: 'encryption_keys',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'SET NULL'
        },
        preview_status: {
            type: DataTypes.ENUM('PENDING', 'GENERATED', 'FAILED', 'NOT_SUPPORTED'),
            defaultValue: 'PENDING'
        },
        backup_status: {
            type: DataTypes.ENUM('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED'),
            defaultValue: 'PENDING'
        },
        version_number: {
            type: DataTypes.INTEGER,
            defaultValue: 1
        },
        parent_file_id: {
            type: DataTypes.UUID,
            allowNull: true,
            references: {
                model: 'files',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'SET NULL',
            comment: 'For file versioning'
        },
        metadata: {
            type: DataTypes.JSON,
            allowNull: true,
            defaultValue: null
        },
        is_deleted: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        uploaded_at: {
            type: DataTypes.DATE,
            defaultValue: DataTypes.NOW
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
                fields: ['file_hash']
            },
            {
                fields: ['backup_status']
            },
            {
                fields: ['preview_status']
            },
            {
                fields: ['parent_file_id']
            },
            {
                unique: true,
                fields: ['user_id', 'file_path']
            }
        ]
    });

    // Instance methods
    File.prototype.isCompleted = function() {
        return this.backup_status === 'COMPLETED';
    };

    File.prototype.hasPreviews = function() {
        return this.preview_status === 'GENERATED';
    };

    File.prototype.canGeneratePreview = function() {
        const previewableTypes = [
            'image/', 'video/', 'application/pdf',
            'text/', 'application/json'
        ];
        return previewableTypes.some(type => this.mime_type.startsWith(type));
    };

    return File;
};