const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const FilePreview = sequelize.define('file_previews', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
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
        preview_type: {
            type: DataTypes.ENUM('THUMBNAIL', 'SMALL', 'MEDIUM', 'LARGE'),
            allowNull: false
        },
        width: {
            type: DataTypes.INTEGER,
            allowNull: true
        },
        height: {
            type: DataTypes.INTEGER,
            allowNull: true
        },
        file_size: {
            type: DataTypes.BIGINT,
            allowNull: true
        },
        storage_path: {
            type: DataTypes.TEXT,
            allowNull: false
        },
        mime_type: {
            type: DataTypes.STRING(100),
            allowNull: false
        },
        generated_at: {
            type: DataTypes.DATE,
            defaultValue: DataTypes.NOW
        }
    }, {
        indexes: [
            {
                fields: ['file_id']
            },
            {
                fields: ['preview_type']
            },
            {
                unique: true,
                fields: ['file_id', 'preview_type']
            }
        ]
    });

    return FilePreview;
};