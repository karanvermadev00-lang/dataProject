-- Init schema for local development / Dockerized Airflow.
-- Source table: herbarium_tasks
-- Target tables: ci_herbarium_specimen (parent) and ci_specimen_dtl (versioned child).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE herbarium_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    image_url TEXT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    task_type VARCHAR(50) NOT NULL DEFAULT 'IMAGE_UPLOAD',
    status VARCHAR(30) NOT NULL DEFAULT 'RAW_UPLOAD',
    created_by_id BIGINT,
    assigned_to_id BIGINT,
    validated_by_id BIGINT,
    genus VARCHAR(100),
    species VARCHAR(100),
    family VARCHAR(100),
    common_name VARCHAR(255),
    collector_name VARCHAR(255),
    collection_date DATE,
    location VARCHAR(255),
    taxonomy_data TEXT,
    metadata TEXT,
    notes TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    assigned_at TIMESTAMP WITHOUT TIME ZONE,
    transcription_completed_at TIMESTAMP WITHOUT TIME ZONE,
    validated_at TIMESTAMP WITHOUT TIME ZONE,
    collection_number VARCHAR(255),
    accession_number VARCHAR(255),
    accession_date DATE,
    department VARCHAR(255),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude DOUBLE PRECISION,
    barcode VARCHAR(255),
    determiner VARCHAR(255),
    habitat VARCHAR(500)
);

CREATE TABLE ci_herbarium_specimens (
    id UUID PRIMARY KEY,
    pk_hrb_specimen_id VARCHAR(25),
    introduction_date DATE,
    introduction_time VARCHAR(50),
    collection_date DATE,
    collection_time VARCHAR(50),
    specimen_name VARCHAR(255),
    vernacular_name VARCHAR(255),
    group_details TEXT,
    group_name VARCHAR(255),
    group_image VARCHAR(255),
    family_name VARCHAR(255),
    family_details TEXT,
    genius_name VARCHAR(255), -- Note: typically spelled 'genus_name'
    genius_details TEXT,
    species_name VARCHAR(255),
    species_details TEXT,
    type_name VARCHAR(255),
    type_details TEXT,
    subspecies_variety VARCHAR(255),
    accession_number VARCHAR(50),
    collection_no VARCHAR(50),
    collector_number VARCHAR(50),
    collector_name VARCHAR(255),
    collector_designation VARCHAR(255),
    collector_email VARCHAR(255),
    determiner_number VARCHAR(50),
    determiner_name VARCHAR(255),
    determiner_designtion VARCHAR(255), -- Keeping 'designtion' as per your list
    determiner_email VARCHAR(255),
    country_name VARCHAR(255),
    country_population VARCHAR(50),
    country_area VARCHAR(50),
    country_flag VARCHAR(255),
    country_language VARCHAR(255),
    state_name VARCHAR(255),
    state_area VARCHAR(50),
    state_population VARCHAR(50),
    district_name VARCHAR(255),
    district_area VARCHAR(50),
    district_population VARCHAR(50),
    locality_name VARCHAR(255),
    collection_latitude VARCHAR(50),
    collection_longitude VARCHAR(50),
    collection_altitude VARCHAR(50),
    icon_image VARCHAR(255),
    original_image VARCHAR(255),
    specimen_details TEXT,
    specimen_status VARCHAR(10),
    barcode VARCHAR(103),
    taxonomy_data TEXT,
    milvus_entity_id BIGINT,
    segment_a_done_at TIMESTAMP WITHOUT TIME ZONE,
    segment_b_done_at TIMESTAMP WITHOUT TIME ZONE,
    segment_c_done_at TIMESTAMP WITHOUT TIME ZONE
);

-- ETL sync fields used by the Airflow DAG.
ALTER TABLE ci_herbarium_specimens
    ADD COLUMN IF NOT EXISTS seq_num_current INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS latest_source_task_id UUID,
    ADD COLUMN IF NOT EXISTS taxonomy_metadata JSONB,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS ci_specimen_dtl (
    specimen_id UUID NOT NULL,
    seq_num INTEGER NOT NULL,
    source_task_id UUID NOT NULL,
    icon_image TEXT NOT NULL,
    lablel_image TEXT,
    taxonomy_metadata JSONB NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (specimen_id, seq_num),
    UNIQUE (specimen_id, source_task_id)
);

ALTER TABLE ci_specimen_dtl
    ADD COLUMN IF NOT EXISTS lablel_image TEXT;

CREATE INDEX IF NOT EXISTS ci_specimen_dtl_source_task_id_idx ON ci_specimen_dtl (source_task_id);
CREATE INDEX IF NOT EXISTS ci_specimen_dtl_specimen_seq_idx ON ci_specimen_dtl (specimen_id, seq_num DESC);

-- Dead letter table for corrupted / unreadable source images.
-- One row per source_task_id; populated by DAG tasks.
CREATE TABLE IF NOT EXISTS ci_specimen_image_dlq (
    source_task_id UUID PRIMARY KEY,
    specimen_id UUID,
    error_text TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ci_specimen_image_dlq_specimen_id_idx ON ci_specimen_image_dlq (specimen_id);

-- Relationships for ERD visibility and referential integrity.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ci_specimen_dtl_specimen'
    ) THEN
        ALTER TABLE ci_specimen_dtl
            ADD CONSTRAINT fk_ci_specimen_dtl_specimen
            FOREIGN KEY (specimen_id)
            REFERENCES ci_herbarium_specimens (id)
            ON UPDATE CASCADE
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ci_specimen_dtl_source_task'
    ) THEN
        ALTER TABLE ci_specimen_dtl
            ADD CONSTRAINT fk_ci_specimen_dtl_source_task
            FOREIGN KEY (source_task_id)
            REFERENCES herbarium_tasks (id)
            ON UPDATE CASCADE
            ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ci_specimen_image_dlq_source_task'
    ) THEN
        ALTER TABLE ci_specimen_image_dlq
            ADD CONSTRAINT fk_ci_specimen_image_dlq_source_task
            FOREIGN KEY (source_task_id)
            REFERENCES herbarium_tasks (id)
            ON UPDATE CASCADE
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ci_specimen_image_dlq_specimen'
    ) THEN
        ALTER TABLE ci_specimen_image_dlq
            ADD CONSTRAINT fk_ci_specimen_image_dlq_specimen
            FOREIGN KEY (specimen_id)
            REFERENCES ci_herbarium_specimens (id)
            ON UPDATE CASCADE
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ci_herbarium_specimens_latest_source_task'
    ) THEN
        ALTER TABLE ci_herbarium_specimens
            ADD CONSTRAINT fk_ci_herbarium_specimens_latest_source_task
            FOREIGN KEY (latest_source_task_id)
            REFERENCES herbarium_tasks (id)
            ON UPDATE CASCADE
            ON DELETE SET NULL;
    END IF;
END $$;

-- Seed sample rows into the source task table for local development.
-- The DAG will migrate only rows where status = 'COMPLETED'.

INSERT INTO herbarium_tasks (id,image_url,filename,task_type,status,created_by_id,assigned_to_id,validated_by_id,genus,species,"family",common_name,collector_name,collection_date,"location",taxonomy_data,metadata,notes,created_at,updated_at,assigned_at,transcription_completed_at,validated_at,collection_number,accession_number,accession_date,department,latitude,longitude,altitude,barcode,determiner,habitat) VALUES
	 ('1417693e-b2d9-4f14-bff8-b40f84af6504'::uuid,'/api/v1/images/93fcb9fa-bb81-478a-8e77-3c69dde5d919-010826.jpg','010826.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Hispide','Moraceae','','K.K. Singh & Kushal Kumar','1998-06-01','Kangra Valley (H.P)','{"barcode":"LWG000010826","barcodeDigits":"","genus":"Ficus","species":"Hispide","family":"Moraceae","commonName":"","collectorName":"K.K. Singh & Kushal Kumar","location":"Kangra Valley (H.P)","collectionDate":"1998-06-01","collectionNumber":"EBH-10018","accessionNumber":"88214","accessionDate":"1999-11-16","determiner":"K.K. Singh","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 10:53:57.894976','2026-03-03 04:11:14.649778','2026-03-02 04:22:08.604344','2026-03-02 04:26:37.792733','2026-03-03 04:11:14.649498','EBH-10018','88214','1999-11-16',NULL,NULL,NULL,NULL,'LWG000010826','K.K. Singh',NULL),
	 ('76246c22-a216-45b1-a47c-932067c19795'::uuid,'/api/v1/images/fe94546a-32a5-4ded-88a4-544782ba50ba-010827.jpg','010827.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'FicusHu','Hispide ','Moraceae','','K.K. Singh & Kushal Kumar','1999-06-01','Kangra Valley (H.P)','{"barcode":"LWG000010827","barcodeDigits":"","genus":"FicusHu","species":"Hispide ","family":"Moraceae","commonName":"","collectorName":"K.K. Singh & Kushal Kumar","location":"Kangra Valley (H.P)","collectionDate":"1999-06-01","collectionNumber":"EBH-10018","accessionNumber":"88213","accessionDate":"1999-11-16","determiner":"K.K. Singh","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.030109','2026-03-03 04:11:15.541034','2026-03-02 04:22:08.607699','2026-03-02 04:28:26.363824','2026-03-03 04:11:15.540861','EBH-10018','88213','1999-11-16',NULL,NULL,NULL,NULL,'LWG000010827','K.K. Singh',NULL),
	 ('7fa681f0-25b3-40bc-b2e0-14e011b8e85f'::uuid,'/api/v1/images/e5e32b85-4f68-4e58-8a3d-3cc741039726-010828.jpg','010828.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'FicusHisp','Hispida','Moraceae','','K.K. Singh & Kushal Kumar','1999-06-01','Kangra Valley (H.P)','{"barcode":"LWG000010828","barcodeDigits":"","genus":"FicusHisp","species":"Hispida","family":"Moraceae","commonName":"","collectorName":"K.K. Singh & Kushal Kumar","location":"Kangra Valley (H.P)","collectionDate":"1999-06-01","collectionNumber":"EBH-100","accessionNumber":"88212","accessionDate":"1999-11-16","determiner":"K.K. Singh","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.122189','2026-03-03 04:11:16.495491','2026-03-02 04:22:08.625345','2026-03-02 04:34:24.449233','2026-03-03 04:11:16.49515','EBH-100','88212','1999-11-16',NULL,NULL,NULL,NULL,'LWG000010828','K.K. Singh',NULL),
	 ('4edf93a2-ba55-4519-b981-703af72d63c9'::uuid,'/api/v1/images/af47726c-a78d-428b-be5c-17ab606623da-010829.jpg','010829.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Hispida','Moraceae','','R. Tiwari , L.B. Chaudhary& A.K. Kushwaha','2014-07-08','Dist Bilaspur, Himachal Pradesh','{"barcode":"LWG000010829","barcodeDigits":"","genus":"Ficus","species":"Hispida","family":"Moraceae","commonName":"","collectorName":"R. Tiwari , L.B. Chaudhary& A.K. Kushwaha","location":"Dist Bilaspur, Himachal Pradesh","collectionDate":"2014-07-08","collectionNumber":"264588","accessionNumber":"","accessionDate":"","determiner":"Rinkey Tiwari","latitude":"31°21.361","longitude":"76°45.690","altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.237194','2026-03-03 04:11:17.281802','2026-03-02 04:22:08.605809','2026-03-02 04:32:54.005931','2026-03-03 04:11:17.28157','264588','',NULL,NULL,NULL,NULL,NULL,'LWG000010829','Rinkey Tiwari',NULL),
	 ('d3cef045-719d-41cb-a9f0-51cd9b2f5301'::uuid,'/api/v1/images/cea5ccd0-1af9-4586-98f1-77f61e71a994-010830.jpg','010830.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Hispida','Moraceae','','R. Tiwari , L.B. Chaudhary& A.K. Kushwaha','2014-07-08','Dist Bilaspur, Himachal Pradesh','{"barcode":"LWG000010830","barcodeDigits":"","genus":"Ficus","species":"Hispida","family":"Moraceae","commonName":"","collectorName":"R. Tiwari , L.B. Chaudhary& A.K. Kushwaha","location":"Dist Bilaspur, Himachal Pradesh","collectionDate":"2014-07-08","collectionNumber":"264588","accessionNumber":"101974","accessionDate":"2016-07-14","determiner":"Rinkey Tiwari","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.350754','2026-03-03 04:11:17.954729','2026-03-02 04:22:08.611598','2026-03-02 04:35:38.099243','2026-03-03 04:11:17.954506','264588','101974','2016-07-14',NULL,NULL,NULL,NULL,'LWG000010830','Rinkey Tiwari',NULL),
	 ('a971f0b5-5560-4106-ba43-fc02c5de1736'::uuid,'/api/v1/images/bd0e2d53-aaaf-4dc4-a2ce-d4dc982bb726-010831.jpg','010831.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Sarmentosa','Moraceae','','R. Tiwari , L.B. Chaudhary& A.K. Kushwaha','2014-08-06','Manali, Himachal Pradesh','{"barcode":"LWG000010831","barcodeDigits":"","genus":"Ficus","species":"Sarmentosa","family":"Moraceae","commonName":"","collectorName":"R. Tiwari , L.B. Chaudhary& A.K. Kushwaha","location":"Manali, Himachal Pradesh","collectionDate":"2014-08-06","collectionNumber":"264573","accessionNumber":"102303","accessionDate":"2014-08-06","determiner":"Rinkey Tiwari","latitude":"32°10.65","longitude":"77°10.770","altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.428858','2026-03-03 04:11:18.678773','2026-03-02 04:22:08.628975','2026-03-02 04:38:16.89465','2026-03-03 04:11:18.67849','264573','102303','2014-08-06',NULL,NULL,NULL,NULL,'LWG000010831','Rinkey Tiwari',NULL),
	 ('18bdec2f-c732-4ba5-9b7e-5c00dcf6fa98'::uuid,'/api/v1/images/d13805e5-aae4-4a09-b496-ce4a63d83793-010832.jpg','010832.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Sarmentosa','Moraceae','','R. Tiwari , L.B. Chaudhary& A.K. Kushwaha','2014-08-06','Manali, Himachal Pradesh','{"barcode":"LWG000010832","barcodeDigits":"","genus":"Ficus","species":"Sarmentosa","family":"Moraceae","commonName":"","collectorName":"R. Tiwari , L.B. Chaudhary& A.K. Kushwaha","location":"Manali, Himachal Pradesh","collectionDate":"2014-08-06","collectionNumber":"264573","accessionNumber":"102301","accessionDate":"2016-07-26","determiner":"Rinkey Tiwari","latitude":"32°10.651","longitude":"77°10.770","altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.497364','2026-03-03 04:11:19.379029','2026-03-02 04:22:08.67223','2026-03-02 04:44:58.330762','2026-03-03 04:11:19.378786','264573','102301','2016-07-26',NULL,NULL,NULL,NULL,'LWG000010832','Rinkey Tiwari',NULL),
	 ('727c789a-19cc-4fcc-a96a-abf0682c2763'::uuid,'/api/v1/images/e2b8acb3-6908-4e97-8841-5366867093b0-010833.jpg','010833.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Sarmentosa','Moraceae','','R. Tiwari , L.B. Chaudhary& A.K. Kushwaha','2014-08-06','Manali, Himachal Pradesh','{"barcode":"LWG000010833","barcodeDigits":"","genus":"Ficus","species":"Sarmentosa","family":"Moraceae","commonName":"","collectorName":"R. Tiwari , L.B. Chaudhary& A.K. Kushwaha","location":"Manali, Himachal Pradesh","collectionDate":"2014-08-06","collectionNumber":"264673","accessionNumber":"102305","accessionDate":"2017-07-26","determiner":"Rinkey Tiwari","latitude":"32°10.651","longitude":"77°10.770","altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.563918','2026-03-03 04:11:20.045364','2026-03-02 04:22:08.672296','2026-03-02 04:42:38.576928','2026-03-03 04:11:20.045118','264673','102305','2017-07-26',NULL,NULL,NULL,NULL,'LWG000010833','Rinkey Tiwari',NULL),
	 ('67d08d50-0a77-43df-8b94-66e53e75a3a1'::uuid,'/api/v1/images/2d6da713-d6f7-48ef-a8c4-35e060e0b3f2-010834.jpg','010834.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Pumila','Moraceae','','J.G. Srivastava & Party','1959-06-02','Dalhousia','{"barcode":"LWG000010834","barcodeDigits":"","genus":"Ficus","species":"Pumila","family":"Moraceae","commonName":"","collectorName":"J.G. Srivastava & Party","location":"Dalhousia","collectionDate":"1959-06-02","collectionNumber":"60781","accessionNumber":"42935","accessionDate":"1960-07-30","determiner":"Hira Lal","latitude":null,"longitude":null,"altitude":"","notes":""}','{}','','2026-02-21 10:53:58.651911','2026-03-03 04:11:21.889747','2026-03-02 04:22:08.672394','2026-03-02 04:53:33.046052','2026-03-03 04:11:21.889537','60781','42935','1960-07-30',NULL,NULL,NULL,NULL,'LWG000010834','Hira Lal',NULL),
	 ('5cc286f6-4593-45de-8761-20d648975d02'::uuid,'/api/v1/images/469c6e59-a572-497d-8868-c2f9bf94f147-010835.jpg','010835.jpg','IMAGE_UPLOAD','COMPLETED',29,12,7,'Ficus','Pumila','Moraceae','','Prof. K.N. Kaul & Party','1963-10-04','Simla , Between Shaltu Rampur, Himachal Pradesh','{"barcode":"LWG000010835","barcodeDigits":"","genus":"Ficus","species":"Pumila","family":"Moraceae","commonName":"","collectorName":"Prof. K.N. Kaul & Party","location":"Simla , Between Shaltu Rampur, Himachal Pradesh","collectionDate":"1963-10-04","collectionNumber":"61990","accessionNumber":"52144","accessionDate":"1965-01-18","determiner":"H.L. Yadav","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 10:53:58.737701','2026-03-03 04:11:22.61911','2026-03-02 04:22:08.673868','2026-03-02 04:47:58.260321','2026-03-03 04:11:22.618911','61990','52144','1965-01-18',NULL,NULL,NULL,NULL,'LWG000010835','H.L. Yadav',NULL);
INSERT INTO herbarium_tasks (id,image_url,filename,task_type,status,created_by_id,assigned_to_id,validated_by_id,genus,species,"family",common_name,collector_name,collection_date,"location",taxonomy_data,metadata,notes,created_at,updated_at,assigned_at,transcription_completed_at,validated_at,collection_number,accession_number,accession_date,department,latitude,longitude,altitude,barcode,determiner,habitat) VALUES
	 ('4849e3f0-0955-42ab-b01e-1d216368d8fc'::uuid,'/api/v1/images/fe3eba68-7de5-4488-8b9e-746af624de0b-048974.jpg','048974.jpg','IMAGE_UPLOAD','ASSIGNED',29,52,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:21.693057','2026-03-02 05:00:04.242538','2026-03-02 05:00:04.242307',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('24ba85f1-ec10-4dd5-b354-b8a401ab52e8'::uuid,'/api/v1/images/417d8aa4-b151-40d7-a460-66d74c3c4f9c-048975.jpg','048975.jpg','IMAGE_UPLOAD','ASSIGNED',29,52,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:21.745221','2026-03-02 05:00:04.242504','2026-03-02 05:00:04.242249',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('2c2fcb11-ffab-41d1-a4b5-402d31865251'::uuid,'/api/v1/images/651dd5e7-bc81-497b-ae24-dfb0f4739d06-049133.jpg','049133.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.219329','2026-03-02 07:44:06.923071','2026-03-02 07:44:06.92267',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('121b1dc4-ede7-4933-a754-ae7737ff266b'::uuid,'/api/v1/images/2da87b6a-8dcb-4591-9d9c-5262185e16d3-049134.jpg','049134.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.264939','2026-03-02 07:44:06.923766','2026-03-02 07:44:06.923474',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('6101fe6d-550d-4b69-813e-56597b3468fc'::uuid,'/api/v1/images/acc397e2-617f-4469-901b-513935d1d73b-049135.jpg','049135.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.316824','2026-03-02 07:44:06.923397','2026-03-02 07:44:06.923023',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('a2f4d6df-ed9e-4d9d-be8f-9fd970bd36d1'::uuid,'/api/v1/images/b9b9f0a6-c247-49ee-9ff5-88e30d25872f-049136.jpg','049136.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.372677','2026-03-02 07:44:06.923065','2026-03-02 07:44:06.922671',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('40d21584-23f6-4936-a807-58a5b6b5abb3'::uuid,'/api/v1/images/80f1846c-9d45-42c4-91c0-1fb912fca536-049137.jpg','049137.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.426812','2026-03-02 07:44:06.924042','2026-03-02 07:44:06.923741',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('14faae2e-15c4-46e6-a210-b25585db8adb'::uuid,'/api/v1/images/1962066e-be3b-479b-9ae5-d0fd6f379312-049138.jpg','049138.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.485951','2026-03-02 07:44:06.931495','2026-03-02 07:44:06.931146',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('65337d9d-9e5c-4382-925f-62f2ffa9031e'::uuid,'/api/v1/images/12f32978-a1f9-44a3-acd3-1e1f196fbe46-049139.jpg','049139.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.537698','2026-03-02 07:44:06.931032','2026-03-02 07:44:06.930802',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('e0674ab6-57b5-4354-b072-5e6a518b87aa'::uuid,'/api/v1/images/3529555b-ba97-4714-a723-50b81d2db7e9-049140.jpg','049140.jpg','IMAGE_UPLOAD','ASSIGNED',29,44,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-20 05:46:30.597413','2026-03-02 07:44:06.930255','2026-03-02 07:44:06.930081',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO herbarium_tasks (id,image_url,filename,task_type,status,created_by_id,assigned_to_id,validated_by_id,genus,species,"family",common_name,collector_name,collection_date,"location",taxonomy_data,metadata,notes,created_at,updated_at,assigned_at,transcription_completed_at,validated_at,collection_number,accession_number,accession_date,department,latitude,longitude,altitude,barcode,determiner,habitat) VALUES
	 ('b080ee60-d696-4191-a427-b06f7a971313'::uuid,'/api/v1/images/c5879269-bdec-498d-9b25-5c34e584c7ba-064765.jpg','064765.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum L.','Boraginaceae','','G.Saran  & Party','1956-03-12','Bihar , Rajgir hills','{"groupName":"Angiosperm","barcode":"LWG000064765","barcodeDigits":"","genus":"Heliotropium","species":"indicum L.","family":"Boraginaceae","commonName":"","collectorName":"G.Saran  & Party","location":"Bihar , Rajgir hills","collectionDate":"1956-03-12","collectionNumber":"25679","accessionNumber":"32738","accessionDate":"1959-07-09","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":"Few, at base only"}','{}','Few, at base only','2026-02-20 05:50:36.313816','2026-03-12 08:32:09.818016','2026-03-02 11:39:24.436613','2026-03-12 08:32:09.817872',NULL,'25679','32738','1959-07-09',NULL,NULL,NULL,NULL,'LWG000064765','',NULL),
	 ('ebaee7fe-35ec-4dfe-9607-b8c3f80b3302'::uuid,'/api/v1/images/669be693-5e55-4dde-8be3-e631bc7ff1cd-064766.jpg','064766.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum L','Boraginaceae','','J.G.Srivastava & Party','1958-03-11','Someshwar Hill , N.Bihar','{"groupName":"Angiosperm","barcode":"LWG000064766","barcodeDigits":"","genus":"Heliotropium","species":"indicum L","family":"Boraginaceae","commonName":"","collectorName":"J.G.Srivastava & Party","location":"Someshwar Hill , N.Bihar","collectionDate":"1958-03-11","collectionNumber":"48649","accessionNumber":"33849","accessionDate":"1959-08-31","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.359369','2026-03-12 08:35:28.280588','2026-03-02 11:39:27.42313','2026-03-12 08:35:28.280165',NULL,'48649','33849','1959-08-31',NULL,NULL,NULL,NULL,'LWG000064766','',NULL),
	 ('7c34220b-f07c-4f7e-8b98-29e8e8578b54'::uuid,'/api/v1/images/d96bda2e-edf4-43fa-b905-f042b5166970-064767.jpg','064767.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum','Boraginaceae','','J.G.Srivastava & Party','1958-03-19','Galgolia , North Purnima, Bihar & Bengal''s Border','{"groupName":"Angiosperm","barcode":"LWG000064767","barcodeDigits":"","genus":"Heliotropium","species":"indicum","family":"Boraginaceae","commonName":"","collectorName":"J.G.Srivastava & Party","location":"Galgolia , North Purnima, Bihar & Bengal''s Border","collectionDate":"1958-03-19","collectionNumber":"49153","accessionNumber":"34584","accessionDate":"1959-08-19","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.405525','2026-03-12 08:46:04.503661','2026-03-02 11:39:27.422106','2026-03-12 08:46:04.503432',NULL,'49153','34584','1959-08-19',NULL,NULL,NULL,NULL,'LWG000064767','',NULL),
	 ('68c61eea-f323-4727-9c85-f81abecdd5b6'::uuid,'/api/v1/images/3e065baa-eaae-479f-9a50-ca947cc4e184-064768.jpg','064768.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum','Boraginaceae','','G.Saran  & Party','1956-03-11','Patna ,kumkran, Bihar','{"groupName":"Angiosperm","barcode":"LWG000064768","barcodeDigits":"","genus":"Heliotropium","species":"indicum","family":"Boraginaceae","commonName":"","collectorName":"G.Saran  & Party","location":"Patna ,kumkran, Bihar","collectionDate":"1956-03-11","collectionNumber":"25628","accessionNumber":"36990","accessionDate":"1959-11-21","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":"flower blue , common"}','{}','flower blue , common','2026-02-20 05:50:36.455225','2026-03-12 08:50:10.595693','2026-03-02 11:39:27.425684','2026-03-12 08:50:10.595444',NULL,'25628','36990','1959-11-21',NULL,NULL,NULL,NULL,'LWG000064768','',NULL),
	 ('9f2d1a6a-5370-47f3-a9ef-7fdf0d8ce3c2'::uuid,'/api/v1/images/8876d855-7ffd-4127-ba84-8eafb50f47f6-064769.jpg','064769.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum','Boraginaceae','','G.Saran & Party','1956-03-12','Bihar , Rajgir hills','{"groupName":"Angiosperm","barcode":"LWG000064769","barcodeDigits":"","genus":"Heliotropium","species":"indicum","family":"Boraginaceae","commonName":"","collectorName":"G.Saran & Party","location":"Bihar , Rajgir hills","collectionDate":"1956-03-12","collectionNumber":"25679","accessionNumber":"35965","accessionDate":"1959-09-30","determiner":"Hiralal","latitude":null,"longitude":null,"altitude":null,"notes":"Few at line Base"}','{}','Few at line Base','2026-02-20 05:50:36.506066','2026-03-12 08:55:30.562385','2026-03-02 11:39:27.425715','2026-03-12 08:55:30.562139',NULL,'25679','35965','1959-09-30',NULL,NULL,NULL,NULL,'LWG000064769','Hiralal',NULL),
	 ('55a2761d-bffd-4405-88e3-58e10401899d'::uuid,'/api/v1/images/89ccd758-c807-456e-aaac-2faaaa65fa3b-064770.jpg','064770.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','ovalifolium','Boraginaceae','','J.G.Srivastava ',NULL,'','{"groupName":"Angiosperm","barcode":"LWG000064770","barcodeDigits":"","genus":"Heliotropium","species":"ovalifolium","family":"Boraginaceae","commonName":"","collectorName":"J.G.Srivastava ","location":"","collectionDate":"","collectionNumber":"21110","accessionNumber":"9238","accessionDate":"1955-08-30","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.55579','2026-03-12 09:01:34.215317','2026-03-02 11:39:27.426786','2026-03-12 09:01:34.215176',NULL,'21110','9238','1955-08-30',NULL,NULL,NULL,NULL,'LWG000064770','',NULL),
	 ('5d9da3a6-13c6-4e02-8359-d88b20e888f3'::uuid,'/api/v1/images/efe12150-c070-4b37-9ad4-d9a0454c2418-064771.jpg','064771.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','ovalifolium','Boraginaceae','','J.G.Srivastava & Party','1957-12-21','Kumhrar, Patna , Bihar','{"groupName":"Angiosperm","barcode":"LWG000064771","barcodeDigits":"","genus":"Heliotropium","species":"ovalifolium","family":"Boraginaceae","commonName":"","collectorName":"J.G.Srivastava & Party","location":"Kumhrar, Patna , Bihar","collectionDate":"1957-12-21","collectionNumber":"46593","accessionNumber":"33682","accessionDate":"1959-07-30","determiner":"Hiralal","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.607997','2026-03-12 09:04:57.30907','2026-03-02 11:39:27.434261','2026-03-12 09:04:57.308839',NULL,'46593','33682','1959-07-30',NULL,NULL,NULL,NULL,'LWG000064771','Hiralal',NULL),
	 ('ac45ac46-2470-4587-89b0-eaae0ec0536e'::uuid,'/api/v1/images/dce70c77-66c5-4338-b830-0195f0b71df4-064772.jpg','064772.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','indicum','Boraginaceae','','G.S.S & Party','1957-12-21','Kumhrar, Patna , Bihar','{"groupName":"Angiosperm","barcode":"LWG000064772","barcodeDigits":"","genus":"Heliotropium","species":"indicum","family":"Boraginaceae","commonName":"","collectorName":"G.S.S & Party","location":"Kumhrar, Patna , Bihar","collectionDate":"1957-12-21","collectionNumber":"46593","accessionNumber":"34706","accessionDate":"1959-08-20","determiner":"Hiralal","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.656661','2026-03-12 09:07:20.678244','2026-03-02 11:39:27.438451','2026-03-12 09:07:20.677932',NULL,'46593','34706','1959-08-20',NULL,NULL,NULL,NULL,'LWG000064772','Hiralal',NULL),
	 ('76e66557-f012-4aab-bf1e-fb2fa542ce0f'::uuid,'/api/v1/images/5086ee5a-e457-4ed6-8672-8b08bfa4b454-064773.jpg','064773.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,61,NULL,'Heliotropium','ovalifolium , ','Boraginaceae','','G.Saran &  Party','1956-03-12','Bihar, 32 miles from patna towards Rajgir Hills','{"groupName":"Angiosperm","barcode":"LWG000064773","barcodeDigits":"","genus":"Heliotropium","species":"ovalifolium , ","family":"Boraginaceae","commonName":"","collectorName":"G.Saran &  Party","location":"Bihar, 32 miles from patna towards Rajgir Hills","collectionDate":"1956-03-12","collectionNumber":"25932","accessionNumber":"28469","accessionDate":"1958-10-11","determiner":"Hiralal / R.Dayal","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-20 05:50:36.704008','2026-03-12 09:11:02.851699','2026-03-02 11:39:27.437462','2026-03-12 09:11:02.85152',NULL,'25932','28469','1958-10-11',NULL,NULL,NULL,NULL,'LWG000064773','Hiralal / R.Dayal',NULL),
	 ('d817743d-adde-44b4-a12e-e87a43bece6d'::uuid,'/api/v1/images/5e577592-c63e-4582-a6e0-4e1b419b84d7-100560.jpg','100560.jpg','IMAGE_UPLOAD','PENDING_REVIEW',29,40,NULL,'Globba','racemosa','Zingiberaceae','','B.K. Nayer & Party','1958-06-05','Basistaram, Kamrup, Assam ','{"groupName":"Angiosperm","barcode":"LWG000100560","barcodeDigits":"","genus":"Globba","species":"racemosa","family":"Zingiberaceae","commonName":"","collectorName":"B.K. Nayer & Party","location":"Basistaram, Kamrup, Assam ","collectionDate":"1958-06-05","collectionNumber":"51458","accessionNumber":"36724","accessionDate":"1959-01-26","determiner":"","latitude":null,"longitude":null,"altitude":null,"notes":""}','{}','','2026-02-21 11:07:37.712422','2026-03-12 06:55:34.444677','2026-03-02 11:46:42.44816','2026-03-12 06:55:34.444467',NULL,'51458','36724','1959-01-26',NULL,NULL,NULL,NULL,'LWG000100560','',NULL);
INSERT INTO herbarium_tasks (id,image_url,filename,task_type,status,created_by_id,assigned_to_id,validated_by_id,genus,species,"family",common_name,collector_name,collection_date,"location",taxonomy_data,metadata,notes,created_at,updated_at,assigned_at,transcription_completed_at,validated_at,collection_number,accession_number,accession_date,department,latitude,longitude,altitude,barcode,determiner,habitat) VALUES
	 ('7ddf74ae-e364-4661-92a4-90889ddfae68'::uuid,'/api/v1/images/c6b46de0-6262-459c-801c-2d4fabfb067c-121584.jpg','121584.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:50.749322','2026-02-21 11:54:50.749322',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('a333991e-55f1-4100-8132-4319f2ab8291'::uuid,'/api/v1/images/1b7495a0-86ce-4396-adc6-feef920a9f20-121585.jpg','121585.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:50.828809','2026-02-21 11:54:50.82881',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('c3940155-41a9-4ff8-8df1-d7f43c2cbc84'::uuid,'/api/v1/images/cfd9cfd7-dd81-4aef-a41b-a4a2cb7894fe-121586.jpg','121586.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:50.923615','2026-02-21 11:54:50.923615',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('39e5e09d-fb26-4d39-80a4-8085cf53397b'::uuid,'/api/v1/images/8bec0c2e-18c2-4998-8105-71a7fb0ba2cc-121587.jpg','121587.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.014665','2026-02-21 11:54:51.014665',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('c43f32b3-4037-4d20-baf7-76afb2551ea6'::uuid,'/api/v1/images/315504f3-1fe8-41c6-8914-a80c64cd8066-121588.jpg','121588.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.104121','2026-02-21 11:54:51.104121',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('750783a2-095a-400e-a8eb-dcfe7201e781'::uuid,'/api/v1/images/fc274648-2117-460d-a950-19e40f15c77f-121589.jpg','121589.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.19299','2026-02-21 11:54:51.19299',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('4a32990f-1948-47ae-9d99-9ec7e4e2cb94'::uuid,'/api/v1/images/649cae64-d2af-4f45-8512-4ff39d97b7e2-121590.jpg','121590.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.288999','2026-02-21 11:54:51.288999',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('89fb794c-2198-4afe-9680-81fef48d78b1'::uuid,'/api/v1/images/cb3352fe-6390-4b8e-83ba-c1990cd65858-121591.jpg','121591.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.370262','2026-02-21 11:54:51.370262',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('a90470c6-eaad-4ad3-b290-16e98ecbc291'::uuid,'/api/v1/images/7a8e794c-5650-4f45-bab8-dbbb61f49899-121592.jpg','121592.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.460991','2026-02-21 11:54:51.460991',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
	 ('c23d1ac2-bef2-4ae7-b1d1-02a66cee2c41'::uuid,'/api/v1/images/45450250-3cbd-43fb-b59a-5ee2cc7cd5b2-121593.jpg','121593.jpg','IMAGE_UPLOAD','RAW_UPLOAD',29,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-02-21 11:54:51.547198','2026-02-21 11:54:51.547198',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);

